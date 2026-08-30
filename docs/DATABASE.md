# THE DROP — Database Design

PostgreSQL 16 + pgvector. Single database `thedrop`. Owned exclusively by `services/api` (ADR-0006).

Conventions:
- `snake_case` tables and columns, plural table names.
- Primary keys: `BIGSERIAL` internally; every externally-visible row also carries a `public_id UUID` so internal IDs never leak into URLs or APIs.
- All timestamps `TIMESTAMPTZ`, stored UTC. `created_at` / `updated_at` on every table.
- Soft delete only where an audit trail requires it (`Article`, `MediaAsset`); everything else hard-deletes under retention policy.
- Enums are Postgres native `ENUM` types where the value set is stable, `TEXT + CHECK` where it is likely to grow.
- Money in `NUMERIC(12,6)` (AI costs are sub-cent). Never floats.

---

## 1. Entity map

```
Provider 1---* Source 1---* RawArticle *---1 Story
                                  |             |
                                  |             |---* StorySource
                                  |             |---* Claim ---* ClaimEvidence
                                  |             |---* ViralSignal
                                  |             |---* StoryEntity *---1 Entity
                                  |             |---1 Article
                                  |
Article 1---* ArticleVersion                    Article *---1 Category
Article 1---* ArticleSourceRef                  Article *---* Tag
Article 1---* MediaAsset                        Article 1---* FactCheck
Article 1---* VideoAsset                        Article 1---* EditorialReview
Article 1---1 Publication 1---* PublicationQueue
Article 1---* PerformanceMetric
Article 1---* Correction
Article 1---* AffiliateLink

Job (desktop lease) *---1 Story|Article
AIRun *---1 PromptVersion,  AIRun *---1 Job
Trend 1---* ViralSignal
Experiment, AuditLog, NewsletterSubscriber, AdPlacement, WorkerNode  (standalone)
```

---

## 2. Ingestion domain

### `providers`
The adapter registry. One row per integration, not per feed.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `slug` | text unique | `gnews`, `newsapi`, `rss`, `govfeed`, `manual`, `trends` |
| `display_name` | text | |
| `adapter_class` | text | dotted path resolved at runtime |
| `enabled` | boolean | |
| `config` | jsonb | non-secret adapter config |
| `credential_ref` | text | key name in the secret store, **never the secret** |
| `rate_limit_per_hour` | int | |
| `quota_used_today` | int | reset by `maintain` |
| `default_reliability` | numeric(4,3) | 0–1 baseline for sources from this provider |
| `circuit_state` | enum | `closed` / `open` / `half_open` |
| `circuit_opened_at` | timestamptz | |
| `last_success_at`, `last_error_at`, `last_error` | | |

### `sources`
A publisher/outlet. Distinct from provider: GNews may deliver articles from 400 sources.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `domain` | text unique | canonical registrable domain |
| `name` | text | |
| `homepage_url` | text | |
| `country`, `language` | text | |
| `source_type` | enum | `wire`, `national`, `local`, `government`, `academic`, `trade`, `blog`, `aggregator`, `social`, `unknown` |
| `reliability_score` | numeric(4,3) | 0–1, maintained by the credibility framework |
| `reliability_basis` | jsonb | inputs and last recomputation |
| `bias_label` | text null | stored, **never** used to suppress; used for balance reporting |
| `is_primary_authority` | boolean | true for `.gov`, courts, regulators, official orgs |
| `allow_auto_publish` | boolean | if false, stories relying solely on it cannot auto-publish |
| `robots_policy` | jsonb | crawl/attribution constraints observed |

Indexes: `sources(domain)`, `sources(reliability_score DESC)`.

### `raw_articles`
Immutable capture of an ingested item. Never edited after insert.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `public_id` | uuid unique | |
| `provider_id` | fk providers | |
| `source_id` | fk sources | |
| `story_id` | fk stories null | assigned by clustering |
| `canonical_url` | text | after redirect + tracking-param stripping |
| `url_hash` | bytea | sha256 of `canonical_url`, **unique** |
| `original_url` | text | as delivered |
| `title` | text | |
| `dek` | text null | |
| `body_text` | text null | extracted, plaintext |
| `body_html_sanitized` | text null | sanitized, for quote extraction |
| `authors` | text[] | |
| `published_at` | timestamptz | source-reported |
| `discovered_at` | timestamptz | our clock |
| `language` | text | |
| `image_urls` | text[] | **references only**, never rehosted |
| `raw_payload` | jsonb | full provider response for replay |
| `simhash` | bigint | 64-bit, title + first 400 chars of body |
| `content_hash` | bytea | sha256 of normalized body |
| `embedding` | vector(384) null | written by desktop, null until then |
| `embedded_at` | timestamptz null | |
| `dedup_status` | enum | `pending`, `unique`, `near_duplicate`, `exact_duplicate` |
| `duplicate_of_id` | fk raw_articles null | |
| `injection_flags` | jsonb | prompt-injection scan results (SECURITY.md §6) |
| `ingest_status` | enum | `raw`, `normalized`, `clustered`, `rejected` |
| `reject_reason` | text null | |

Indexes:
- `UNIQUE (url_hash)` — the primary dedup guard.
- `raw_articles(discovered_at DESC)`, `raw_articles(story_id)`, `raw_articles(dedup_status) WHERE dedup_status='pending'`.
- `raw_articles(simhash)` for Hamming-bucket lookup.
- HNSW: `USING hnsw (embedding vector_cosine_ops)` with `WHERE embedding IS NOT NULL`.
- `GIN (to_tsvector('english', title || ' ' || coalesce(body_text,'')))`.

> Retention: `raw_payload` is dropped after 30 days for rows not attached to a published article. `body_text` retained 180 days. This keeps the largest table bounded.

---

## 3. Story domain

### `stories`
A real-world event, one row, many source articles.

| Column | Type | Notes |
|---|---|---|
| `id`, `public_id` | | |
| `title` | text | working title, not the headline |
| `summary` | text null | |
| `category_id` | fk categories | primary category |
| `centroid` | vector(384) null | running cluster centroid |
| `status` | enum | `discovered`, `clustering`, `scoring`, `extracting`, `verifying`, `ready_to_write`, `writing`, `qa`, `approved`, `published`, `rejected`, `deferred` |
| `source_count` | int | distinct `sources` in cluster |
| `independent_source_count` | int | excludes syndication of the same wire copy |
| `first_seen_at`, `last_activity_at` | timestamptz | |
| `us_relevance_score` | smallint | 0–100 |
| `viral_score` | smallint | 0–100 |
| `opportunity_score` | smallint | 0–100 |
| `importance_score` | smallint | 0–100 |
| `credibility_score` | smallint | 0–100 |
| `verification_confidence` | smallint null | 0–100, from the verification pass |
| `risk_tier` | enum | `standard`, `elevated`, `high` — drives the stricter rules |
| `risk_reasons` | text[] | e.g. `{death, legal_accusation}` |
| `scores_computed_at` | timestamptz null | |
| `known_unknowns` | jsonb | open questions carried into the packet |
| `contradictions` | jsonb | conflicting reports across sources |
| `evidence_packet` | jsonb null | the frozen packet handed to Claude |
| `evidence_packet_hash` | bytea null | reproducibility |
| `rejected_reason` | text null | |
| `defer_until` | timestamptz null | |

Indexes: `stories(status, last_activity_at DESC)`, `stories(opportunity_score DESC) WHERE status='ready_to_write'`, `stories(category_id, published…)`, HNSW on `centroid`.

### `story_sources`
Join with cluster metadata.

`story_id`, `raw_article_id`, `similarity numeric(5,4)`, `is_primary boolean`, `is_syndicated boolean`, `added_at`. Unique `(story_id, raw_article_id)`.

### `entities` / `story_entities`
`entities`: `id`, `canonical_name`, `entity_type` (`PERSON|ORG|PLACE|EVENT|PRODUCT|LEGISLATION|OTHER`), `aliases text[]`, `wikidata_id null`, `is_public_figure`, `sensitivity` (`normal|elevated`, e.g. minors, victims).
`story_entities`: `story_id`, `entity_id`, `salience numeric(4,3)`, `mention_count`. Unique pair.

---

## 4. Claim and verification domain

### `claims`
Atomic, checkable statements extracted from the cluster.

| Column | Type | Notes |
|---|---|---|
| `id`, `public_id` | | |
| `story_id` | fk stories | |
| `claim_text` | text | one assertion, no conjunctions |
| `claim_type` | enum | `FACT`, `CLAIM`, `ALLEGATION`, `OPINION`, `ANALYSIS`, `PREDICTION`, `PROJECTION`, `OFFICIAL_STATEMENT`, `UNVERIFIED` |
| `attributed_to_entity_id` | fk entities null | who asserted it — required for `CLAIM`/`ALLEGATION` |
| `confidence` | smallint | 0–100 |
| `verification_status` | enum | `unverified`, `single_source`, `corroborated`, `authoritative`, `disputed`, `refuted` |
| `is_load_bearing` | boolean | true if the headline or lede depends on it |
| `supporting_source_count` | int | distinct independent sources |
| `contradicted_by` | jsonb | claim ids + source refs |
| `first_asserted_at` | timestamptz | |
| `verified_at` | timestamptz null | |
| `verifier_ai_run_id` | fk ai_runs null | traceability |

Constraint (enforced in app + a DB `CHECK`): a claim may only be rendered as fact in an article when `verification_status IN ('corroborated','authoritative')`. Everything else must be attributed in prose. See PIPELINE.md §9.

### `claim_evidence`
`claim_id`, `raw_article_id`, `source_id`, `quote text`, `quote_offset int null`, `url`, `stance` (`supports|contradicts|context`), `is_primary_document boolean`, `document_url null`, `weight numeric(4,3)`.

### `fact_checks`
Per-article verification record: `article_id`, `claim_id null`, `check_type` (`cross_source|primary_doc|numeric|quote_fidelity|entity_identity|temporal`), `result` (`pass|warn|fail`), `detail jsonb`, `ai_run_id null`, `checked_at`.

---

## 5. Article domain

### `categories`
`id`, `slug` unique, `name`, `description`, `parent_id null`, `sort_order`, `is_active`, `target_articles_per_day int null`, `seo_title`, `seo_description`, `accent_token text` (design token name, not a hex value).

Seeded: trending, politics, entertainment, sports, business, technology, world. Adding a category is one row plus a revalidation — no code change.

### `tags`
`id`, `slug` unique, `name`, `usage_count`, `is_trending`. Join `article_tags(article_id, tag_id)`.

### `articles`

| Column | Type | Notes |
|---|---|---|
| `id`, `public_id` | | |
| `story_id` | fk stories | |
| `slug` | text | unique with the date path |
| `category_id` | fk categories | |
| `article_type` | enum | `NEWS`, `ANALYSIS`, `OPINION`, `COMMENTARY`, `BREAKING`, `EXPLAINER`, `LIVE` |
| `headline` | text | |
| `alternate_headlines` | jsonb | candidates + their scores |
| `dek` | text | |
| `body_mdx` | text | structured markdown, sanitized |
| `body_blocks` | jsonb | block representation for rendering and ad insertion |
| `key_facts` | jsonb | bullet takeaways |
| `byline` | text | e.g. "The Drop Newsroom" |
| `author_id` | fk authors null | |
| `word_count`, `reading_time_seconds` | int | |
| `status` | enum | `draft`, `qa`, `approved`, `scheduled`, `published`, `updated`, `unpublished`, `rejected` |
| `editorial_confidence` | smallint | 0–100 — the publishing gate input |
| `qa_report` | jsonb | rule-by-rule QA results |
| `risk_tier` | enum | inherited from story, may be raised |
| `published_at`, `updated_at_public` | timestamptz null | |
| `first_published_at` | timestamptz null | immutable once set |
| `seo_title`, `meta_description` | text | |
| `og_title`, `og_description` | text | |
| `structured_data` | jsonb | NewsArticle JSON-LD, generated not hand-written |
| `hero_media_id` | fk media_assets null | |
| `canonical_url` | text | |
| `noindex` | boolean | corrections/duplicates |
| `is_sponsored` | boolean | |
| `disclosure_text` | text null | |
| `view_count`, `share_count` | bigint | denormalized counters |
| `deleted_at` | timestamptz null | soft delete |

Indexes: `UNIQUE(category_id, slug)`; `articles(status, published_at DESC)`; `articles(published_at DESC) WHERE status='published'`; `articles(category_id, published_at DESC)`; GIN on `to_tsvector(headline || dek || body)`; GIN on `structured_data`.

URL path is derived, not stored: `/{category.slug}/{YYYY}/{MM}/{DD}/{slug}` from `first_published_at`.

### `article_versions`
Full immutable snapshot on every material change: `article_id`, `version int`, `headline`, `body_mdx`, `changed_fields jsonb`, `change_reason`, `changed_by` (`system|ai|user_id`), `ai_run_id null`, `created_at`. Unique `(article_id, version)`.

### `article_source_refs`
Attribution shown to readers: `article_id`, `source_id`, `raw_article_id null`, `url`, `title`, `publisher`, `accessed_at`, `ref_type` (`reporting|primary_document|data|quote`), `display_order`.

### `editorial_reviews`
`article_id`, `reviewer` (`rules|ai_primary|ai_secondary|human`), `ai_run_id null`, `verdict` (`pass|revise|reject`), `score smallint`, `findings jsonb`, `created_at`.

### `corrections`
`article_id`, `correction_type` (`correction|clarification|update|retraction`), `summary`, `detail`, `field_changed null`, `previous_value null`, `issued_at`, `issued_by`, `is_public boolean`. Public corrections render on the article and on `/corrections`.

---

## 6. Publishing domain

### `publications`
One row per article that reaches live: `article_id` unique, `published_at`, `publish_decision` (`auto_high|auto_verified|auto_second_review|manual`), `gate_confidence smallint`, `gate_config_snapshot jsonb` (the thresholds in force at decision time — critical for audits), `revalidated_at`, `sitemap_included boolean`.

### `publication_queue`
`article_id`, `action` (`publish|update|unpublish|revalidate|distribute`), `scheduled_for`, `status` (`pending|running|done|failed`), `attempts`, `last_error`, `payload jsonb`. Index on `(status, scheduled_for)`.

---

## 7. Viral intelligence domain

### `trends`
External signal rows: `id`, `topic`, `normalized_topic`, `provider_id`, `region` (default `US`), `metric_type` (`search_volume|rising_query|social_mentions|reddit_score`), `value numeric`, `rank int null`, `captured_at`, `raw jsonb`. Index `(normalized_topic, captured_at DESC)`.

### `viral_signals`
Time series per story, so velocity is measurable rather than guessed.

`id`, `story_id`, `signal_type` (`source_count|publication_velocity|search_trend|reddit_discussion|social_mentions|breaking_indicator|entity_momentum`), `value numeric`, `normalized_value smallint` (0–100), `weight numeric(4,3)`, `window_minutes int`, `captured_at`, `provider_id null`, `raw jsonb`.

Index `(story_id, captured_at DESC)`, `(signal_type, captured_at DESC)`.

Velocity is computed as the derivative across consecutive rows — which is exactly why signals are stored as a series and scores are stored on `stories` as a materialized snapshot.

---

## 8. Media domain

### `media_assets`
`id`, `public_id`, `article_id null`, `story_id null`, `asset_role` (`hero|social|vertical|breaking_card|inline|thumbnail`), `storage_key` (path/key, not URL), `mime_type`, `width`, `height`, `bytes`, `blurhash`, `alt_text` (required to publish), `caption`, `credit`, `rights_status` enum (`ORIGINAL_AI|LICENSED|PUBLIC_DOMAIN|VALIDATED_CC|UNKNOWN|PROHIBITED`), `license_ref null`, `is_ai_generated boolean`, `ai_disclosure_text null`, `generator_model`, `prompt_version_id null`, `prompt_text`, `negative_prompt null`, `seed bigint null`, `generation_params jsonb`, `generated_at`, `usage_status` (`draft|approved|published|rejected|archived`), `safety_report jsonb`, `cost numeric(12,6)`.

Hard rule enforced in the publish gate: only `ORIGINAL_AI`, `LICENSED`, `PUBLIC_DOMAIN`, `VALIDATED_CC` may auto-publish.

### `video_assets`
`id`, `public_id`, `article_id`, `format` (`vertical_1080x1920`), `duration_seconds`, `storage_key`, `poster_media_id null`, `script jsonb` (scene beats with per-scene claim ids), `script_verified boolean`, `voice_provider`, `voice_id`, `captions_vtt_key`, `render_engine`, `render_params jsonb`, `rights_status`, `usage_status`, `qa_report jsonb`, `cost numeric(12,6)`, `rendered_at`.

---

## 9. AI, cost and prompt domain

### `prompt_versions`
`id`, `name` (`article_generate`, `claim_extract`, …), `version int`, `template text`, `variables jsonb`, `model_hint`, `is_active boolean`, `checksum bytea`, `notes`, `created_at`. Unique `(name, version)`. Exactly one active version per name, enforced by a partial unique index.

### `ai_runs`
Every model call, without exception.

`id`, `job_id null`, `story_id null`, `article_id null`, `prompt_version_id null`, `purpose` (`extract|score|verify|write|qa|headline|image_prompt|video_script|other`), `provider` (`anthropic|ollama|other`), `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost numeric(12,6)`, `latency_ms`, `status` (`ok|error|refused|invalid_output`), `error null`, `request_digest bytea` (hash, not the prompt), `response_meta jsonb`, `created_at`.

Index `(created_at DESC)`, `(model, created_at DESC)`, `(article_id)`.

> Full prompts and completions are **not** stored by default — only digests plus a bounded excerpt when `status != 'ok'`. Source text can contain injected instructions and personal data; retaining it wholesale is a liability. Verbose capture is a per-environment flag, off in production.

### `model_pricing`
`model`, `input_per_mtok numeric`, `output_per_mtok numeric`, `cache_read_per_mtok`, `cache_write_per_mtok`, `effective_from`, `effective_to null`.

Seeded from configuration at deploy time — **rates are not hardcoded in application code and are not invented in this document.** Fill them from the current Anthropic pricing page before enabling cost gates.

### `budgets`
`scope` (`daily|monthly|category|job_type`), `scope_key null`, `limit_amount numeric`, `spent_amount numeric`, `period_start`, `period_end`, `action_on_breach` (`warn|throttle|halt`), `is_breached boolean`.

### `jobs`
The desktop work queue. Not Celery — this is the durable lease table.

`id`, `public_id`, `job_type`, `priority smallint`, `payload jsonb`, `story_id null`, `article_id null`, `idempotency_key text unique`, `status` (`queued|leased|done|failed|cancelled`), `attempts int`, `max_attempts int`, `available_at timestamptz`, `leased_by fk worker_nodes null`, `leased_at null`, `lease_expires_at null`, `heartbeat_at null`, `result jsonb null`, `error null`, `created_at`, `completed_at null`.

Index: `jobs(status, priority DESC, available_at) WHERE status='queued'` — the claim query's covering index.

### `worker_nodes`
`id`, `name`, `token_hash bytea`, `token_rotated_at`, `capabilities jsonb`, `status` (`online|degraded|offline`), `last_heartbeat_at`, `current_job_count`, `gpu_name`, `gpu_vram_free_mb`, `agent_version`, `ip_last_seen inet`, `registered_at`.

---

## 10. Analytics, growth and revenue

### `performance_metrics`
Rolled up, not raw events: `article_id null`, `metric_date date`, `metric_type` (`views|unique_views|avg_scroll_pct|avg_read_seconds|ctr|shares|video_completion_pct|search_impressions|search_clicks`), `value numeric`, `dimension jsonb` (`{source: "google_news"}`), unique `(article_id, metric_date, metric_type, dimension)`.

Raw pageviews land in Redis, flush to a partitioned `page_events` table every 60 s, and are rolled up hourly. `page_events` partitions older than 35 days are dropped.

### `experiments`
`id`, `name`, `hypothesis`, `metric`, `baseline jsonb`, `guardrails jsonb`, `variant_config jsonb`, `branch_name`, `status` (`proposed|approved|running|analyzing|adopted|rejected|rolled_back`), `started_at`, `ended_at`, `result jsonb`, `approved_by null`.

### `newsletter_subscribers`
`email citext unique`, `status` (`pending|confirmed|unsubscribed|bounced`), `confirm_token_hash`, `confirmed_at null`, `unsubscribed_at null`, `source`, `preferences jsonb`, `provider_ref null` (external ESP id).

### `affiliate_links`
`id`, `article_id null`, `partner`, `campaign null`, `target_url`, `tracking_url`, `disclosure_text`, `is_active`, `clicks bigint`, `conversions bigint`, `revenue numeric(12,2)`, `created_at`.

### `ad_placements`
`id`, `slot_key` (`header|after_intro|mid_article|sidebar|article_end|home_module_1`), `provider` (`adsense|direct|house|none`), `is_active`, `config jsonb`, `categories text[] null`, `article_types text[] null`, `excluded_risk_tiers text[]` (blocks ads on sensitive stories), `priority`.

### `audit_logs`
`id`, `actor_type` (`user|system|worker|ai`), `actor_id null`, `action`, `entity_type`, `entity_id`, `before jsonb null`, `after jsonb null`, `ip inet null`, `user_agent null`, `request_id`, `created_at`. Append-only; no UPDATE or DELETE grant for the app role. Partitioned monthly, retained 400 days.

### `users` / `roles`
`users`: `id`, `email citext unique`, `password_hash` (argon2id), `display_name`, `is_active`, `mfa_secret_enc null`, `mfa_enabled`, `last_login_at`, `failed_login_count`, `locked_until null`, `subscription_tier` enum (`FREE|REGISTERED|PREMIUM`, default `FREE`).
`roles`: `admin`, `editor`, `analyst`, `viewer`. Join `user_roles`. Phase 1 seeds a single `admin`.

---

## 11. Index and performance policy

- Every foreign key gets an index. No exceptions — Postgres does not create them.
- Partial indexes for hot filtered queries (`WHERE status='queued'`, `WHERE status='published'`).
- HNSW (`m=16, ef_construction=64`) on `raw_articles.embedding` and `stories.centroid`. Build **after** initial backfill, not before.
- `page_events` and `audit_logs` are range-partitioned by month.
- `raw_articles` will be the largest table (~2–5 k rows/day). Partition by `discovered_at` month once it passes ~2 M rows; the retention sweep may make that unnecessary.
- Tuning for 8 GB with everything co-resident: `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=16MB`, `maintenance_work_mem=256MB`, `max_connections=60` (with a pool cap well below it), `random_page_cost=1.1` (SSD).

## 12. Connection budget

| Consumer | Pool |
|---|---|
| FastAPI (2 uvicorn workers × 5) | 10 + 5 overflow |
| Celery worker (concurrency 2) | 4 + 2 overflow |
| Alembic / ops | 5 |
| Headroom | rest |

Well inside `max_connections=60`. No PgBouncer in Phase 1; add it only if pooling pressure appears.

## 13. Migrations

Alembic, one head, forward-only in production. Every migration must be reversible in staging. Destructive changes are two-phase: deploy the additive migration, deploy code, then deploy the drop in a later release. Migrations run as a `systemd` `ExecStartPre` on the API unit and are idempotent under re-run.

## 14. Affiliate domain (Phase 5B — see AFFILIATE_ENGINE.md)

### `affiliate_merchants`
`id`, `slug` unique, `name`, `domain`, `network` (`amazon|impact|cj|shareasale|rakuten|walmart|bestbuy|direct|other`), `adapter_slug`, `logo_media_id null`, `commission_notes`, `allows_page_fetch boolean` (false where terms forbid it — the adapter enforces this), `image_rights` (`api_licensed|prohibited|unknown`), `is_active`, `config jsonb`, `credential_ref text` (secret-store key, never the secret).

### `affiliate_products`
`id`, `public_id`, `merchant_id` fk, `product_ref` (merchant SKU/ASIN/id), `name`, `brand null`, `category_id` fk categories null, `product_category text` (merchant taxonomy), `description text null`, `specifications jsonb`, `fields jsonb` — **the provenance map**: every attribute stored as `{value, source, confidence, fetched_at}` per ADR-0009 — `price_amount numeric null`, `price_currency null`, `price_source text null`, `price_fetched_at null`, `rating_value null`, `rating_count null`, `rating_source null`, `availability null`, `availability_fetched_at null`, `primary_image_media_id null`, `status` enum (`ACTIVE|INACTIVE|NEEDS_METADATA|LINK_ERROR|EXPIRED`), `missing_fields text[]`, `target_audience null`, `added_by` fk users, `created_at`, `last_refreshed_at`.

Unique `(merchant_id, product_ref)`. Index on `status`, on `category_id`.

> `fields` is the source of truth for rendering; the flattened columns exist for querying and sorting only. A renderer that reads a flattened column without checking provenance is a bug, and a test asserts the generator never does.

### `affiliate_links`
`id`, `public_id`, `product_id` fk, `campaign_id` fk null, `original_url` (exactly as pasted, tracking params preserved), `destination_url null` (after resolution), `destination_domain null`, `tracking_url` (our `/go/{public_id}`), `network_tracking_ids jsonb`, `status` (`ok|redirected|broken|expired|timeout|unchecked`), `last_checked_at null`, `consecutive_failures int`, `clicks bigint`, `impressions bigint`, `is_active`, `created_at`.

### `affiliate_campaigns`
`id`, `slug` unique, `name`, `merchant_id null`, `starts_at null`, `ends_at null`, `notes`, `is_active`, `clicks`, `conversions`, `revenue numeric(12,2)`.

### `affiliate_articles`
Joins the commerce workflow to the standard `articles` table rather than duplicating it.

`id`, `article_id` fk articles unique null (null until generated), `commercial_type` enum (`PRODUCT_REVIEW|BUYING_GUIDE|BEST_PRODUCTS_LIST|PRODUCT_COMPARISON|PRODUCT_ROUNDUP|GIFT_GUIDE|BEST_FOR_GUIDE|TRENDING_PRODUCT|NEWS_PLUS_RECOMMENDATION|HOW_TO|DEALS`), `angle_rationale jsonb` (why this type was chosen), `primary_keyword null`, `target_audience null`, `ranking_criteria jsonb null` (required for any "best" list), `status` enum (`DRAFT|GENERATING|QUALITY_CHECK|READY|SCHEDULED|PUBLISHED|FAILED`), `scheduled_for null`, `publish_mode` (`draft|auto|schedule`), `disclosure_id` fk, `qa_report jsonb`, `failure_reason null`, `created_by` fk users.

Join `affiliate_article_products(affiliate_article_id, product_id, display_order, verdict_note null)` — one row per product in a roundup.

### `affiliate_ctas`
`id`, `affiliate_article_id` fk, `product_id` fk, `link_id` fk, `placement` enum (`after_intro|after_overview|after_features|before_verdict|article_end|product_card`), `button_text`, `button_variant`, `disclosure_mode` (`banner|inline|both`), `is_visible boolean` (auto-false when the link is unhealthy), `impressions bigint`, `clicks bigint`, `display_order`.

### `affiliate_clicks`
Append-only, partitioned monthly: `id`, `link_id` fk, `cta_id null`, `article_id null`, `campaign_id null`, `merchant_id`, `clicked_at`, `referrer_class` (`internal|search|social|direct|other`), `device_class`, `country null`, `ip_truncated inet null`, `session_hash null`, `is_bot boolean`.

Rolled up hourly into `performance_metrics`; raw partitions dropped after 90 days.

### `affiliate_conversions`
`id`, `link_id null`, `campaign_id null`, `merchant_id`, `external_order_ref null`, `occurred_at`, `commission numeric(12,2)`, `currency`, `status` (`pending|approved|reversed`), `import_source` (`postback|csv|manual`), `raw jsonb`.

Populated only from what a network actually reports. No estimation.

### `affiliate_disclosures`
`id`, `slug`, `text`, `version int`, `placement_default` (`banner|inline|both`), `is_active`, `created_at`. Versioned so an article records which disclosure text it published with.

### `affiliate_cta_templates`
`id`, `name`, `button_text_template`, `condition jsonb` (which data availability triggers it — e.g. fresh API price → "Check Latest Price"), `variant`, `is_active`, `priority`.

### `affiliate_link_health_checks`
`id`, `link_id` fk, `checked_at`, `http_status null`, `final_url null`, `redirect_count`, `latency_ms`, `result` (`ok|redirected|broken|expired|timeout`), `detail null`. Index `(link_id, checked_at DESC)`; retained 90 days.

**Cross-domain constraint:** a database `CHECK` plus an application rule forbids any `affiliate_ctas` or `affiliate_links` row associating with an `articles` row whose `article_type` is `NEWS`, `ANALYSIS`, `OPINION` or `COMMENTARY`. Commercial and editorial content stay separable at the schema level, not just by policy.

---

## 15. Backups

- Nightly `pg_dump --format=custom` to `/var/backups/thedrop/`, 14 daily + 8 weekly retained, plus an off-box copy.
- WAL archiving enabled for PITR once the site carries real traffic.
- Restore is rehearsed, not assumed — the runbook in DEPLOYMENT.md §9 includes a restore drill.
