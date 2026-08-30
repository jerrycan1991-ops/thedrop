# THE DROP — Roadmap

Nine phases. Each has an explicit exit criterion. A phase is not complete while a critical test fails.

Sequencing principle: **the site is live and boring before it is smart.** Ingestion without a site produces nothing; a site without ingestion is still a site.

---

## Phase 0 — Design and architecture ✅ (this phase)

**Delivered:** ARCHITECTURE.md, DATABASE.md, PIPELINE.md, MEDIA_PIPELINE.md, SECURITY.md, DEPLOYMENT.md, MONETIZATION.md, ROADMAP.md, TASKS.md, ADR-0001…0008, CLAUDE.md, proposed repo tree.

**Exit:** operator approves the architecture and the repository tree. No code written.

---

## Phase 1 — Website foundation

Monorepo, design system, public site with placeholder content, admin shell with real auth, FastAPI with real health checks, Postgres + pgvector + Redis, migrations, CI.

**Exit criteria**
- `https://thedrop.channel` serves the homepage over the existing nginx proxy, from `127.0.0.1:3100`, with **no nginx change**.
- Dark/light/system theming works; all colors come from tokens.
- All 15 required routes exist and render (placeholder content is fine).
- An article renders at `/{category}/{yyyy}/{mm}/{dd}/{slug}` from the database.
- Admin login works with a real session; `/admin` is inaccessible unauthenticated.
- `/healthz` and `/readyz` green; migrations at head.
- Test suite green: theme tokens, routing, auth, DB, health.
- Restore drill completed once.

**Deliberately not in Phase 1:** any AI, any provider, any real content, any ads.

---

## Phase 2 — News ingestion

Provider interface and adapters (GNews, RSS, Government feeds, Manual), normalization, cheap dedup, scheduling, provider health/circuit breakers, worker lease API, desktop `agent-runner` skeleton with heartbeat.

**Exit criteria**
- ≥ 3 providers ingesting on schedule; ≥ 500 `raw_articles`/day.
- Duplicate rate measurable and < 2 % escaping into distinct stories.
- Admin shows Incoming Stories, Sources, Providers, and worker status.
- Desktop registers, heartbeats, and shows ONLINE/OFFLINE correctly; killing it queues jobs safely and resuming drains them.
- Provider tests pass against recorded fixtures (no live API in CI).

---

## Phase 3 — Intelligence

Embeddings on desktop, clustering, US relevance, viral/importance/opportunity scoring, entity + claim extraction, source credibility framework, Viral Radar page.

**Exit criteria**
- Clustering precision/recall measured on a hand-labeled set of ≥ 200 articles; ≥ 0.90 precision.
- Viral Radar shows live scored stories with drill-down into signals.
- Claim extraction produces atomic, typed, attributed claims on a labeled sample.
- **Prompt-injection test corpus passes** — no injected instruction alters extraction output.
- Scores are reproducible and explainable (weights logged with each score).

---

## Phase 4 — Claude generation

Evidence packet assembly, article generator, headline system, political labeling, editorial QA, confidence scoring, publishing gates, first real published articles.

**Exit criteria**
- 20 articles generated end-to-end; every factual sentence traces to a claim id.
- Gate behaves correctly across all four confidence bands and all three risk tiers (tested with fixtures).
- `test_quota_cannot_lower_gate` passes.
- Zero fabricated sources or quotes across a 50-article audit.
- High-risk stories demonstrably require corroboration; a synthetic single-source death report is **rejected**.
- Cost per article measured and within budget.

---

## Phase 5 — SEO and monetization scaffolding

Sitemaps (standard + Google News), RSS, NewsArticle/Organization/BreadcrumbList schema, OG/Twitter cards, canonical handling, first-party analytics, AdSlot abstraction, affiliate framework, newsletter capture.

**Exit criteria**
- Rich Results Test passes for NewsArticle, Organization, BreadcrumbList.
- Sitemaps validate; news sitemap contains only articles < 48 h; `robots.txt` correct.
- Search Console verified, sitemaps submitted, no coverage errors.
- Core Web Vitals: LCP < 2.5 s, CLS < 0.1, INP < 200 ms on article pages (field or lab).
- Ad slots render nothing when disabled and reserve space when enabled; no CLS regression.
- Analytics recording views, scroll, read time.

---

## Phase 5B — Affiliate content automation engine

Merchant/network adapter layer, product ingestion with per-field provenance, `NEEDS_METADATA` queue, angle selection, affiliate article generation, CTA component and placement system, `/go/{id}` click tracking, disclosure system, link health checker, affiliate analytics, and the `AFFILIATE MARKETING` admin section.

See `docs/AFFILIATE_ENGINE.md` and ADR-0009.

**Exit criteria**
- Pasting a single affiliate URL from a metadata-capable merchant produces a complete, published article with CTAs, disclosure, SEO and an original image — no manual writing.
- A URL with unobtainable metadata lands in `NEEDS_METADATA` and produces **no article**, with the missing fields named in the admin queue.
- `test_no_price_without_source` and `test_no_rating_without_source` pass: no currency figure or star rating can appear without tier-1/4 provenance.
- Banned experiential phrases ("I tested", "we tried", "hands-on") are rejected by QA on a fixture corpus.
- No affiliate link can attach to a `NEWS`/`ANALYSIS`/`OPINION`/`COMMENTARY` article — blocked at the database level and tested.
- Every affiliate article renders a disclosure above the fold; a generation that omits it cannot publish.
- A roundup of 5 products builds a comparison table from stored fields only, with stated ranking criteria.
- Link health checker demotes a deliberately broken link within one cycle and hides its CTA.
- Affiliate articles are excluded from the Google News sitemap and included in the standard sitemap.

**Deliberately deferred:** network API adapters (Amazon PA-API, Impact, CJ, ShareASale, Rakuten, Walmart, Best Buy) ship individually as credentials are obtained. Phase 5B ships the `generic` and `manual` adapters and the full pipeline around them.

---

## Phase 6 — Media engine

ComfyUI integration, hero/social/vertical/breaking assets, rights tracking, safety review, data graphics, video script → TTS → render → QA.

**Exit criteria**
- Every published article has a compliant hero image with alt text and correct `rights_status`.
- No asset with `UNKNOWN`/`PROHIBITED` can publish (tested).
- 10 vertical videos rendered end-to-end, captions aligned, every spoken fact traced to a verified claim.
- AI imagery visibly labeled; no photoreal depiction of real people.

---

## Phase 7 — Distribution

Adapters for X, Facebook, Instagram, YouTube (Shorts), TikTok, plus newsletter sending. Each behind one interface, independently disable-able, with documented API requirements and approval processes.

**Exit criteria**
- ≥ 2 platforms posting automatically with correct attribution and links.
- Rate limits and retry/backoff respected; failures do not block publishing.
- Documented, honest list of what each platform actually permits — including where automated posting requires app review, a business account, or is not supported at all. No invented capabilities.

---

## Phase 8 — Self-improvement

Experiment framework, metric baselines, regression guards, reporting, approval workflow.

**Exit criteria**
- Baselines recorded for CTR, engagement, duplicate rate, correction rate, verification failure rate, cost, latency.
- An experiment runs the full loop: hypothesis → branch → change → test → benchmark → documented result → human approval.
- `PROTECTED_SETTINGS` enforcement tested: an experiment attempting to lower a verification threshold is **rejected at creation**.

---

## Phase 9 — Scale and revenue (post-launch)

CDN, media offload to object storage, AdSense live, direct ad sales, subscription implementation, additional categories, possible read-replica.

---

## Realistic sequencing

| Phase | Rough size | Blocking dependencies |
|---|---|---|
| 1 | largest single chunk of code, no external deps | none |
| 2 | moderate | provider API keys |
| 3 | moderate, desktop-heavy | desktop set up, Phase 2 data |
| 4 | moderate | `ANTHROPIC_API_KEY`, Phase 3 |
| 5 | small–moderate | live content from Phase 4 |
| 6 | large, GPU-heavy | ComfyUI + models installed |
| 7 | moderate, mostly external approvals | platform accounts, app review |
| 8 | small | data from Phases 4–6 |

The long poles are **platform API approvals** (Phase 7 — start applications during Phase 5) and **AdSense approval** (needs ~30 days of live content — so it gates on Phase 4 shipping, not on Phase 5 code).

---

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Fabricated or defamatory content published | Existential | Claim traceability, corroboration rules, gates, injection defenses, unpublish path, corrections |
| Google News/Discover never picks us up | Growth stalls | Correct schema, editorial policy pages, original content, disclosure, consistent cadence |
| AdSense rejection or ban | Revenue delayed | Wait for content depth; no ads on high-risk stories; policy pages before applying |
| VPS resource exhaustion | Outage | Lean architecture, memory limits per unit, swap, disk alerts, this whole document |
| AI cost overrun | Burn | Model routing, prompt caching, budgets with halt, per-article cost tracking |
| Desktop offline for days | No new articles | Site unaffected; queue drains on return; alerting |
| Provider API changes or price hikes | Ingestion gap | Provider abstraction; ≥ 3 adapters; RSS as a free floor |
| Copyright complaint | Legal + reputation | Original imagery only, links + short quotes, no rehosting, documented takedown path |
| Prompt injection reaches production | Trust collapse | Output-side validation (SECURITY.md §6.3) — the input filter is not the defense |
