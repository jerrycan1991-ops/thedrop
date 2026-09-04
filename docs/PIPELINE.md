# THE DROP — News Pipeline

From discovery to distribution. Every stage names where it runs (VPS or DESKTOP), what it reads, what it writes, and what makes it fail closed.

**Non-negotiable:** verification outranks engagement at every stage. No quota, score, or deadline may cause publication of unverified material.

---

## 0. Stage map

| # | Stage | Runs on | Trigger |
|---|---|---|---|
| 1 | Source discovery | VPS | beat, hourly |
| 2 | Ingestion | VPS | beat, 5–15 min per provider |
| 3 | Normalization | VPS | inline with ingest |
| 4 | Cheap deduplication | VPS | inline with ingest |
| 5 | Embedding | DESKTOP | job `embed` |
| 6 | Story clustering | VPS (ADR-0015) | inline; consolidation is a desktop job |
| 7 | US relevance scoring | DESKTOP | job `score` |
| 8 | Virality scoring | DESKTOP + VPS signals | job `score` + beat signal capture |
| 9 | Importance scoring | DESKTOP | job `score` |
| 10 | Source credibility | VPS (recompute) | beat, daily |
| 11 | Opportunity scoring | DESKTOP | job `score` |
| 12 | Entity + claim extraction | DESKTOP | job `extract` |
| 13 | Cross-source verification | DESKTOP | job `verify` |
| 14 | Evidence packet assembly | DESKTOP | job `verify` (tail) |
| 15 | Article generation | DESKTOP | job `write` |
| 16 | Editorial QA | DESKTOP (+VPS rules) | job `qa` |
| 17 | Publishing gate | VPS | on QA completion |
| 18 | Media generation | DESKTOP | jobs `image`, `video` |
| 19 | Publication | VPS | queue |
| 20 | Distribution | VPS (+DESKTOP renders) | queue |
| 21 | Performance tracking | VPS | beat |

---

## 1. Source discovery

Maintains the `sources` registry. Any new domain appearing in ingested items is auto-created with `reliability_score` seeded from the provider default and `source_type='unknown'`, then queued for classification.

A source starts **untrusted**: `allow_auto_publish=false` until it has been classified and has a reliability score. New sources therefore contribute context but cannot single-handedly justify a claim.

## 2. Ingestion

Per-provider Celery task. Each adapter implements:

```python
class NewsProvider(Protocol):
    slug: str
    def fetch(self, since: datetime, cursor: str | None) -> ProviderPage: ...
    def normalize(self, item: dict) -> NormalizedItem: ...
    def health(self) -> ProviderHealth: ...
```

`ProviderPage` carries `items`, `next_cursor`, `rate_limit_remaining`. The pipeline depends only on `NormalizedItem` — no downstream code imports a provider module.

Adapters in Phase 2: `GNewsProvider`, `NewsAPIProvider`, `RSSProvider`, `OfficialGovernmentFeedProvider`, `ManualProvider`, `TrendProvider`.

Guards:
- Per-provider rate limit and quota counter in Redis.
- Circuit breaker: 5 consecutive failures opens for 15 min, then half-open with one probe.
- `robots.txt` and terms respected for any direct fetch. We store links and short quotes, never full rehosted bodies for redistribution.
- Response size cap (2 MB) and total-items cap per run.

## 3. Normalization

Produces `NormalizedItem`:
- Canonical URL: follow redirects, strip `utm_*`/`fbclid`/etc., resolve AMP to canonical, lowercase host, drop trailing slash.
- Text extraction (trafilatura-class), HTML sanitized to a strict allow-list.
- Language detect; non-English items are retained but flagged and excluded from generation in Phase 2–4.
- Publish time normalized to UTC; missing timestamps fall back to discovery time and set a `timestamp_estimated` flag.
- **Injection scan** (SECURITY.md §6) runs here, writing `injection_flags`. Flagged content is still stored — it is evidence — but is wrapped and never treated as instruction.

## 4. Cheap deduplication (VPS, no ML)

Three cascading checks, cheapest first:

1. **Exact URL** — `url_hash` unique constraint. Insert conflict → `exact_duplicate`.
2. **Content hash** — sha256 of normalized body catches identical syndication under different URLs.
3. **SimHash** — 64-bit over title + first 400 chars. Candidates within Hamming distance ≤ 3 are near-duplicates. Bucketed by 16-bit bands for lookup.

Anything surviving all three is `unique` and eligible for embedding. This runs in single-digit milliseconds and requires no model on the VPS — the whole reason ML stays on the desktop.

Semantic near-duplicates that survive SimHash are caught later at clustering, which is the correct place for them.

## 5. Embedding (DESKTOP)

Batch job: up to 256 unembedded `raw_articles`. Model `bge-small-en-v1.5`, 384-dim, normalized, input = `title + "\n" + dek + "\n" + body[:2000]`. Results posted back and written to `raw_articles.embedding`.

Single shared vector space, model name and revision pinned in config. Changing the model requires a full backfill and an ADR — vectors from two models are not comparable.

## 6. Story clustering (DESKTOP)

Incremental, online:

1. For each new embedding, query `stories.centroid` for nearest clusters active in the last 48 h (`ORDER BY centroid <=> $1 LIMIT 10`).
2. If best cosine similarity ≥ `CLUSTER_JOIN_THRESHOLD` (default 0.82) **and** entity overlap ≥ 1 shared salient entity → join; update centroid as a running mean.
3. An article that qualifies to join two stories which are **not similar to each
   other** is a digest — a roundup covering several unrelated events — and joins
   neither. It is not a second source for any of them.
4. Else create a new `story`.
5. Periodic consolidation merges stories that are the same event: centroid similarity at or above `CLUSTER_MERGE_THRESHOLD` (higher than the join threshold) **and** a shared discriminative entity — the same guard, so consolidation cannot become a way around it. The older story survives and the absorbed row is kept with `merged_into_id` set, so the merge stays auditable. This is pairwise and runs on the VPS.
6. Immediately after consolidation, a narrower pass reunites a singleton story with a larger story it should have joined, at the ORIGINAL join threshold rather than the merge threshold — the merge threshold's gap between 0.82 and 0.90 otherwise means a pair that could join fresh can never be reunited once split. Scoped deliberately narrow (singleton-into-larger only, never singleton-into-singleton) rather than lowering the merge threshold everywhere. See ADR-0021.
7. Splitting a cluster whose intra-similarity collapsed is a re-partitioning problem, needs HDBSCAN, and is therefore a desktop job (ADR-0015). Not yet built.

Entity overlap is required because embeddings alone happily merge "shooting in Ohio" with "shooting in Nevada". That is a correctness guard, not an optimization.

The shared entity must **discriminate**: `OTHER`-typed entities never qualify, and neither does one appearing in more than `ENTITY_GUARD_MAX_DOC_FRACTION` of the corpus. Measured against the first 152 articles, "United States" appeared in 18% of them — a bare "≥ 1 shared entity" rule would have let any two US stories merge. See ADR-0017.

A story's own masthead is excluded too, while every current member shares one publisher — otherwise a singleton founded by one outlet's article would have that outlet's own name sitting in its guard set, letting any other article merely mentioning it join on self-attribution rather than a shared subject. The filter lifts the moment a second, different outlet joins the story.

Merges are recorded so a story's identity is auditable.

## 7. US relevance (0–100)

Weighted, transparent, tunable in config:

| Signal | Weight |
|---|---|
| US entities (people, orgs, places, agencies) | 0.30 |
| US publisher share in cluster | 0.20 |
| Topic class US-salience (domestic policy, US markets, US sports leagues…) | 0.20 |
| Direct impact on US audiences (policy, prices, safety, travel) | 0.20 |
| US search/trend signal presence | 0.10 |

Below `US_RELEVANCE_MIN` (default 35) a story is not written unless it is World-category and importance ≥ 80.

Implemented so far: US entities and US publisher share only (2 of 5 signals, 50% of the formula's weight), rescaled to fill 0-100 and recorded as such in `stories.us_relevance_basis`. Runs on the VPS, not the desktop — it is pure SQL, and needs no model. See ADR-0018 for why, and for what is still missing.

## 8. Virality (0–100)

Signals are captured as a **time series** (`viral_signals`) so velocity is measured, not guessed.

| Signal | What it measures |
|---|---|
| `source_count` | independent outlets covering it |
| `publication_velocity` | new articles per hour, and its first derivative |
| `search_trend` | rising-query signal for the topic |
| `reddit_discussion` | comment/score growth on relevant subreddits (public API, ToS-compliant) |
| `social_mentions` | only where a legal, API-accessible source exists |
| `breaking_indicator` | wire alerts, official statements, live-blog starts |
| `entity_momentum` | recent coverage growth for the story's top entities |

```
viral_raw   = Σ (normalized_value_i × weight_i)
freshness   = exp(-hours_since_first_seen / HALF_LIFE_HOURS)   # default 8
accel_bonus = clamp(d(velocity)/dt normalized, 0, 15)
VIRAL_SCORE = clamp(round(viral_raw × freshness + accel_bonus), 0, 100)
```

Weights live in `packages/config`, are versioned, and are logged with each score so historical scores remain interpretable.

**Anti-manipulation:** a single source spamming many URLs cannot inflate `source_count` — only distinct `source_id` with non-syndicated content counts. Syndication is detected by content hash and wire attribution.

## 9. Importance, credibility, opportunity

**Importance (0–100)** — editorial weight independent of buzz: scale of impact, number of people affected, institutional significance, irreversibility, public-safety relevance. A quiet regulatory ruling can be importance 85, virality 10, and we should still write it.

**Source credibility (0–100)** — cluster-level: weighted mean of `sources.reliability_score` for contributing sources, boosted by primary-authority presence, penalized by contradiction density and by reliance on `allow_auto_publish=false` sources.

Per-source reliability is recomputed daily from: historical correction rate on our articles that cited them, contradiction rate versus corroborated claims, transparency signals (bylines, corrections policy, ownership disclosure), and manual overrides recorded in `sources.reliability_basis`.

**Opportunity (0–100)** — what to write *next*:

```
OPPORTUNITY = 0.22*viral
            + 0.18*us_relevance
            + 0.15*importance
            + 0.12*search_potential
            + 0.10*freshness
            + 0.08*credibility
            + 0.07*novelty            (are we adding something, or echoing?)
            + 0.05*monetization_fit   (ad-safe category, evergreen value)
            + 0.03*audience_fit
            - competition_penalty     (0-15, saturated coverage)
            - risk_penalty            (0-20, high risk tier & thin evidence)
```

`monetization_fit` is capped at 5 % and **cannot** promote a story past a verification gate. Deliberate: money nudges ordering, never truth.

## 10. Entity and claim extraction (DESKTOP)

Claude (Haiku tier) over the cluster, with source content passed as untrusted data (SECURITY.md §6). Output is a strict JSON schema, validated by Pydantic; invalid output is retried once, then fails the job.

**Implemented so far:** the ollama path only (`services/agent-runner/agent/claims.py`), a deliberate, switchable deviation from Claude Haiku while local-model quality here is still being measured, wired end to end (`thedrop_database.claim_queue` dispatches, `POST /api/v1/worker/claims` persists). See ADR-0020, including its dispatch-window-gate section — extraction has no automatic re-trigger when a story later gains a member, unlike scoring.

Each claim must be **atomic** (one assertion), carry a `claim_type`, and — for `CLAIM`, `ALLEGATION`, `OFFICIAL_STATEMENT` — name the attributed entity. Extraction also records the exact supporting quote and its source for every claim, into `claim_evidence`.

**Risk tier assignment** happens here. A story is `high` if it touches: elections, crime, deaths, legal accusations, health claims, financial-market claims, war/conflict, allegations against named individuals, public safety, or celebrity death/arrest reports. `elevated` for politics generally, named-person disputes, and corporate wrongdoing. Otherwise `standard`.

## 11. Cross-source verification (DESKTOP)

**Implemented so far:** `authoritative`, `corroborated` and `single_source` only, computed deterministically (`thedrop_database.verification`) — no model, runs on the VPS. `disputed` and `refuted` need a semantic judgement about whether two differently-worded claims conflict, which is real model work, not yet built. The `corroborated` rule's "reliability ≥ threshold" clause is also not applied yet, since no source has ever had its `reliability_score` actively computed (PIPELINE.md §9 is not built). See ADR-0022.

Per claim:

| Evidence | Resulting status |
|---|---|
| ≥ 2 independent credible sources agree (distinct `source_id`, non-syndicated, reliability ≥ threshold) | `corroborated` |
| A directly relevant authoritative primary source (`.gov`, court filing, regulator, official org statement, company filing) | `authoritative` |
| 1 source only | `single_source` |
| Sources conflict | `disputed` |
| Contradicted by an authoritative source | `refuted` |

Rules that fail closed:

- A **load-bearing** claim (headline or lede depends on it) in a `high` risk story must be `corroborated` or `authoritative`. Otherwise the story is deferred, not written.
- `Person X claims Y` may never be rendered as `Y happened`. The generator receives claims with their type and attribution attached, and QA re-checks this mechanically against the claim table.
- Deaths, arrests and criminal charges of named individuals require an authoritative source (official statement, court record, or family/representative confirmation reported by ≥2 credible outlets). Never a single aggregator, never social media alone.
- Numbers, dates and quotes are checked verbatim against `claim_evidence` in QA.
- Contradictions are never silently dropped — they go in the packet and, if material, into the article as "reports conflict".

High-risk verification uses the Opus tier and runs a **second, independent pass** with a different prompt and a fresh context, so the second reviewer cannot inherit the first's assumptions.

## 12. Story evidence packet

The frozen, hashed input to generation. Stored on `stories.evidence_packet`.

```jsonc
{
  "story_id": "uuid",
  "generated_at": "…",
  "category": "politics",
  "risk_tier": "high",
  "verified_claims":   [ { "text": "...", "type": "FACT", "status": "corroborated",
                           "sources": [...], "quote": "...", "load_bearing": true } ],
  "attributed_claims": [ { "text": "...", "type": "ALLEGATION",
                           "attributed_to": "Sen. …", "sources": [...] } ],
  "timeline":          [ { "at": "…", "event": "…", "source": "…" } ],
  "entities":          [ { "name": "…", "type": "PERSON", "salience": 0.9 } ],
  "primary_documents": [ { "title": "…", "url": "…", "publisher": "…" } ],
  "conflicting_reports":[ { "claim": "…", "positions": [...] } ],
  "known_unknowns":    [ "Whether X has been charged" ],
  "prior_coverage":    [ { "article_id": "…", "headline": "…", "published_at": "…" } ],
  "trend_context":     { "viral": 71, "velocity": "rising", "search_terms": [...] },
  "audience_relevance":{ "us_relevance": 88, "why": "…" },
  "source_attribution":[ { "publisher": "…", "url": "…", "reliability": 0.86 } ],
  "prohibited":        [ "Do not state cause of death; not confirmed." ]
}
```

The packet contains **no raw source article text as prose to be rewritten** — only claims, quotes with attribution, and structured facts. That is the structural reason the output cannot be a rewrite of one article.

## 13. Article generation (DESKTOP)

Claude (Sonnet tier; Opus for `high` risk) receives:

- **System**: role, house style, labeling rules, hard prohibitions, output schema.
- **Trusted config**: category, target length, article type, tone.
- **Untrusted evidence**: the packet, inside explicit delimiters, marked as data.

Output is strict JSON matching the required field set (headline, alternate_headlines, slug, dek, article_type, category, tags, byline, body, source_references, key_facts, SEO/OG fields, structured data inputs, image brief, editorial confidence). Schema violations retry once, then fail.

Generation rules baked into the prompt **and** re-checked by QA:
- Every factual sentence must trace to a packet claim. No outside knowledge introduced as fact.
- Claim types survive into prose: `ALLEGATION` becomes "alleged", `CLAIM` gets attribution, `PREDICTION` gets hedging.
- `NEWS` articles carry no opinion. `OPINION`/`COMMENTARY` are labeled, and their factual premises must still cite packet claims.
- Known unknowns are stated, not glossed.
- No invented quotes, statistics, dates, or sources — ever.

### Headline system

5–8 candidates, each scored 0–100 on accuracy, clarity, curiosity, search relevance, social potential, and **sensationalism risk** (inverted). Selection:

```
score = 0.35*accuracy + 0.20*clarity + 0.15*search + 0.15*social + 0.15*curiosity
hard filter: accuracy >= 85 AND sensationalism_risk <= 40
```

A candidate failing the hard filter is discarded regardless of its total. The chosen headline must be entailed by the article body — QA checks this.

## 14. Editorial QA

Deterministic rule checks (cheap, VPS-runnable) plus an AI review pass (desktop).

Rule checks: required fields present; slug unique and well-formed; every `source_reference` resolves to a real ingested URL; every number/date/quote in the body appears in `claim_evidence`; no claim rendered as fact unless corroborated/authoritative; label matches content (opinion markers absent from `NEWS`); no boilerplate AI phrasing from a banned-phrase list; reading level and length in range; alt text present on hero; no PII beyond public-figure norms; no unlabeled AI imagery.

AI review returns `verdict`, `score`, and structured `findings`. For `high` risk stories a **second model** reviews independently.

`editorial_confidence` is the composite: verification strength (40 %), source credibility (20 %), rule-check pass rate (20 %), AI review score (20 %), minus penalties for unresolved findings.

## 15. Publishing gate (VPS)

Thresholds are configuration, snapshotted into `publications.gate_config_snapshot` at decision time.

| Confidence | Standard risk | Elevated | High |
|---|---|---|---|
| 95–100 | publish | publish | second review, then publish |
| 85–94 | extra verification pass, then publish | second review, then publish | second review + all load-bearing claims authoritative/corroborated |
| 70–84 | independent second AI review required | second review required | defer |
| < 70 | reject/defer | reject/defer | reject |

Hard blocks, independent of score:
- Any load-bearing claim not `corroborated`/`authoritative` in a `high` story.
- Any media asset with `rights_status` in (`UNKNOWN`, `PROHIBITED`).
- Missing alt text, missing attribution, or a source reference that does not resolve.
- Budget breach with `action_on_breach='halt'`.
- Global `publishing.enabled=false` kill switch.

**Quota never publishes anything.** The daily target (20–30) influences only which queued stories get generation capacity first. If only 14 stories clear the gate, we publish 14. This is asserted by a test (`test_quota_cannot_lower_gate`).

## 16. Publication and distribution

`publication_queue` handles publish → ISR revalidate (`/`, category, article path, `/latest`, sitemaps) → sitemap ping → distribution enqueue. Distribution adapters (Phase 7) share one interface and are each independently disable-able. Updates create an `article_version` and set `updated_at_public`; corrections render publicly and, for retractions, set `noindex`.

## 17. Performance tracking

First-party events → Redis buffer → `page_events` (partitioned) → hourly rollup into `performance_metrics`. Tracks views, scroll depth, read time, CTR by headline variant, referrer class, video completion, category performance. No third-party behavioural trackers; no cross-site identifiers.

---

## 18. Daily rhythm (target 20–30 articles)

| Window (ET) | Activity |
|---|---|
| 05:00–08:00 | Overnight cluster consolidation, morning ingest surge, 6–8 articles for the US morning |
| 08:00–12:00 | Politics/business focus, 6–8 articles |
| 12:00–17:00 | Entertainment/sports/tech, 6–8 articles |
| 17:00–22:00 | Evening wrap, analysis pieces, 4–6 articles |
| Continuous | Breaking-news interrupt: any story with `breaking_indicator` and importance ≥ 80 jumps the queue |

Target mix per day: Politics/US 5–8, Entertainment 3–5, Sports 3–5, Technology 2–4, Business 2–4, World 2–4, Trending dynamic. Enforced as a *soft* scheduler preference, never as a publishing requirement.

## 19. Backpressure

If the desktop is offline or the queue backs up beyond `MAX_QUEUE_AGE_HOURS`, the scheduler stops creating new `write` jobs and keeps only `embed`/`cluster`/`score` flowing, so the Viral Radar stays accurate even when nothing is being written. Stories older than 36 h without generation are auto-deferred; genuinely stale ones are archived.
