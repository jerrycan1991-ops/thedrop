# THE DROP — Affiliate Content Automation Engine

Goal: paste one affiliate URL in the admin dashboard, click Create, and get a complete, accurate, monetization-ready article with CTAs, disclosure, SEO and an original image — without fabricating a single product fact.

Phase: 5B (after SEO/monetization scaffolding, before or alongside the media engine). Schema lands earlier so nothing needs a migration rewrite.

---

## 0. Two things you need to decide up front

**A. Metadata is the hard part, not the writing.** Merchant product data cannot be reliably scraped. Amazon's terms require the Product Advertising API (which itself requires qualifying sales before access), and most large merchants block automated page fetches. So the engine is **API-first, structured-data second, and honest about failure**: if it cannot get trustworthy data, the product is parked at `NEEDS_METADATA` and no article is written. This is the design, not a limitation to work around — inventing specs would be worse than publishing nothing.

**B. Commercial content is kept structurally separate from news.** Scaled affiliate content sitting inside a news domain is the fastest way to damage Google News/Discover standing for the *whole* site. So affiliate articles live under `/picks/*`, carry commercial article types, are excluded from the Google News sitemap, and are visually distinct. The news brand stays clean and the commerce section still ranks in web search on its own merits. Flagging this as the main risk; the engine below is built either way.

---

## 1. Pipeline

```
AFFILIATE URL (admin paste)
   |
   +-> URL PARSE + NETWORK DETECTION      (which network? which merchant? tracking params preserved)
   |
   +-> LINK RESOLUTION                    (guarded fetch, redirect chain, destination domain)
   |
   +-> METADATA EXTRACTION                (adapter: API -> structured data -> og tags)
   |
   +-> PRODUCT VALIDATION                 (confidence per field; missing -> NEEDS_METADATA)
   |
   +-> AI PRODUCT ANALYSIS                (category, audience, use cases, honest limitations)
   |
   +-> ANGLE + KEYWORD SELECTION          (article type chosen from evidence, not guessed)
   |
   +-> ARTICLE GENERATION                 (13-section structure, sourced claims only)
   |
   +-> CTA INSERTION                      (placement rules, not model-chosen positions)
   |
   +-> SEO + SCHEMA                       (Product/FAQ schema WITHOUT fabricated ratings)
   |
   +-> QUALITY CHECK                      (affiliate-specific QA gate)
   |
   +-> PUBLISH | SCHEDULE | DRAFT
```

Runs as desktop jobs (`affiliate_extract`, `affiliate_write`, `affiliate_image`) leased through the same worker protocol as news. Link resolution and health checks run on the VPS as Celery tasks — they are cheap HTTP, not AI.

---

## 2. Network / merchant adapter architecture

Nothing in the engine knows what Amazon is.

```python
class AffiliateNetworkAdapter(Protocol):
    slug: str                                   # 'amazon_paapi', 'impact', 'cj', 'shareasale',
                                                # 'rakuten', 'walmart', 'bestbuy', 'generic'
    def detects(self, url: str) -> bool: ...
    def parse_link(self, url: str) -> ParsedAffiliateLink: ...      # merchant, product ref, tracking ids
    def fetch_product(self, ref: ProductRef) -> ProductMetadata | None: ...
    def build_link(self, ref: ProductRef, campaign: str | None) -> str: ...
    def health(self) -> AdapterHealth: ...
```

`ProductMetadata` carries **per-field provenance and confidence**:

```python
@dataclass
class Field[T]:
    value: T | None
    source: Literal['api', 'structured_data', 'og_tag', 'admin_override', 'unknown']
    confidence: float          # 0-1
    fetched_at: datetime
```

That structure is the whole anti-fabrication mechanism: a field with `value=None` cannot be rendered, and a field with `source='og_tag'` and low confidence is rendered with hedged language or omitted. The generator receives fields, never a blob of prose to embellish.

### Extraction ladder (first success wins, provenance recorded)

| Tier | Method | Trust |
|---|---|---|
| 1 | Official network/product API (PA-API, Impact, CJ, ShareASale, Rakuten, Walmart, Best Buy) | high |
| 2 | `schema.org/Product` JSON-LD on the destination page, where fetching is permitted | medium |
| 3 | OpenGraph / meta tags (`og:title`, `og:image`, `product:price:amount`) | low |
| 4 | Admin manual entry | high (human-asserted) |
| 5 | Nothing usable | `NEEDS_METADATA` — **stop** |

Fetching respects `robots.txt` and the merchant's terms. If an adapter's terms forbid page fetching, only tiers 1 and 4 are enabled for it — enforced in the adapter, not left to operator memory.

**Phase 5B ships:** `generic` (structured-data + OG, for merchants that permit it) and `manual`. Network API adapters are added as you obtain credentials — each is a self-contained module and requires no pipeline change. The system does not pretend to have API access it lacks.

---

## 3. Price, rating and availability policy

These four fields cause most affiliate-content violations, so they get explicit rules:

| Field | Rule |
|---|---|
| **Price** | Stored with `fetched_at`. Rendered **only** if from tier 1 or 4 and under `PRICE_MAX_AGE_HOURS` (default 24). Otherwise the article says "check the current price" and the CTA reads `Check Latest Price`. Never a stale number presented as current. |
| **Discount / deal** | Only from an API that reports it. Never inferred from a strikethrough on a page. |
| **Rating / review count** | Only from tier 1. **Never** written into schema markup unless it came from the merchant API, and never invented. If absent, no rating appears anywhere — no stars, no "highly rated". |
| **Availability** | Only from tier 1, and only with a freshness stamp. |

A test asserts that a `ProductMetadata` with `price.value=None` cannot produce an article body containing a currency figure.

---

## 4. Article generation

### 4.1 Angle selection

The article type is **derived**, not guessed:

| Condition | Chosen type |
|---|---|
| One product, rich metadata, clear category | `PRODUCT_REVIEW` (specification-based, see §4.3) |
| One product, thin metadata | `HOW_TO` or `BEST_FOR_GUIDE` framed around the use case, not the spec sheet |
| 2 products | `PRODUCT_COMPARISON` |
| 3+ products, same category | `BEST_PRODUCTS_LIST` / `PRODUCT_ROUNDUP` |
| 3+ products, mixed categories, seasonal window | `GIFT_GUIDE` |
| Category matches an active trend cluster | `TRENDING_PRODUCT` |
| Linked to a live news story | `NEWS_PLUS_RECOMMENDATION` (strict rules, §7) |
| Verified active deal from API | `DEALS` |

Inputs to the decision: product category, metadata completeness, target audience, search intent for the primary keyword, trend overlap with the news engine, existing internal coverage (do we already have this article?), and competition. Admin selection always overrides.

### 4.2 Required structure

1. Headline — accurate, not superlative unless criteria are stated
2. Introduction
3. Product overview
4. Key features (**only** extracted features, each traceable to a metadata field)
5. Who it may suit
6. Potential limitations (honest, derived from specs and category norms — never invented defects)
7. Buying considerations
8. Comparison / alternatives where data exists
9. Final verdict (a recommendation *framework*, not a testimonial)
10. Affiliate CTA section
11. Affiliate disclosure
12. FAQ
13. Related articles (internal links)

### 4.3 Language contract — enforced by QA, not just by prompt

**Banned phrasings** (regex + classifier, hard fail):
- "I tested", "we tested", "we tried", "in our testing", "hands-on", "after using it for"
- "I found", "I noticed", "in my experience" (first-person experiential)
- Fabricated superlatives without criteria: "the best" with no stated basis
- Invented consensus: "users say", "reviewers agree", "everyone loves" — unless from a tier-1 review field

**Required framings:**
- "Based on the available specifications…"
- "According to the manufacturer…"
- "On paper, this suggests…"
- "Here is what to consider before buying…"
- "We have not tested this product." — rendered as a standing editorial note on every `PRODUCT_REVIEW`

The verdict section recommends *decision criteria* ("if you need X, this spec set fits; if you need Y, look elsewhere"), which is genuinely useful and entirely honest without hands-on testing.

### 4.4 Thin-content guards

Hard gates before an affiliate article can publish: minimum 700 words of substantive body; at least 5 distinct extracted product facts; primary keyword density ≤ 2 %; no paragraph duplicated across our own affiliate articles above a similarity threshold; at least 2 internal links; FAQ answers ≥ 40 words each and non-generic.

If a product's metadata cannot support 5 real facts, it does not get a standalone article — it gets folded into a roundup, or nothing.

---

## 5. CTA system

```tsx
<AffiliateCTA
  product={product}
  merchant={merchant}
  affiliateUrl={link.trackingUrl}
  buttonText="Check Latest Price"
  campaign="headphones-q3"
  placement="after_intro"
  disclosure="inline"
/>
```

- **Placements** (configurable per article type): `after_intro`, `after_overview`, `after_features`, `before_verdict`, `article_end`, plus one per product card in roundups.
- **Button text** is selected by rule from data availability, never by the model: fresh API price → `Check Latest Price`; verified deal → `See Today's Deal`; availability known → `Check Availability`; otherwise `View Product on {merchant}`. Deceptive wording ("Buy now, 90 % off!") is not in the vocabulary.
- **Link handling**: every button points at `/go/{link_public_id}` (first-party 302) so clicks are ours to measure and the destination can be swapped or disabled without editing articles. `rel="sponsored nofollow noopener"` is applied by the component; authors cannot omit it.
- **Impressions** tracked via `IntersectionObserver`, batched, first-party.
- **Broken link behaviour**: if the link's health status is `broken` or `expired`, the component renders a neutral disabled state or hides entirely — it never sends a reader to a dead link.
- **Responsive and accessible**: minimum 44 px tap target, visible focus ring, tokenized colors, `aria-label` naming product and merchant.

---

## 6. Multi-product and roundups

Admin selects N products → Create Article. Each product keeps its own link, tracking id, merchant, CTA and analytics row. The generator:

1. Compares only fields present for **all** products (an incomplete spec is omitted from the table rather than guessed).
2. Identifies meaningful differences, and says so plainly when two products are near-identical.
3. Builds a comparison table from stored fields, rendered deterministically — no model-authored table cells.
4. States the ranking criteria explicitly. A "best" list with no defined criteria fails QA.
5. Generates one original featured image plus a comparison graphic.

---

## 7. Affiliate × news engine

The trend engine may **suggest** a commercial opportunity when a product category genuinely overlaps a story cluster (category match plus entity overlap plus a relevance threshold). It surfaces as a suggestion card in the admin, never as an automatic action.

Hard rules:
- No affiliate link is ever inserted into an article with `article_type='NEWS'`, `ANALYSIS`, `OPINION` or `COMMENTARY`. Enforced at the database level by a check and by a QA test.
- `NEWS_PLUS_RECOMMENDATION` is a distinct commercial type, lives under `/picks`, is labeled commercial, and is excluded from the news sitemap.
- Relevance is required and scored; a low-relevance suggestion is not shown.
- News and commercial content are visually distinguishable: different section, different label chip, disclosure banner.

---

## 8. Images

Per MEDIA_PIPELINE.md rules, with affiliate specifics:

- Default: **original** editorial illustration, category lifestyle image, comparison graphic, or buying-guide graphic. Generated, labeled, `rights_status='ORIGINAL_AI'`.
- Merchant product photography is used **only** when the network's API supplies it with explicit usage rights (most product APIs do grant image use for affiliates). Then `rights_status='LICENSED'`, with `license_ref`, `source` and any attribution requirement stored and rendered.
- Never scrape a product image off a merchant page. There is no code path for it.
- Generated images never depict a fabricated version of the actual product in a way that could mislead — illustrations are category-level and visibly illustrative.

---

## 9. Disclosure

Configurable per `affiliate_disclosures` row, versioned, with a default:

> Disclosure: This article contains affiliate links. If you buy through them, The Drop may earn a commission at no additional cost to you.

Placement: a banner above the fold on every commercial article (before the first CTA, not buried), plus a compact inline note adjacent to CTA blocks, plus a permanent `/affiliate-disclosure` policy page linked from the footer. Rendering is done by the article template, not by the generator — so a disclosure cannot be omitted by a bad generation.

A publish gate blocks any article with an affiliate link and no rendered disclosure.

---

## 10. SEO

Generated per article: SEO title, meta description, slug, OG title/description, canonical, breadcrumbs, internal-link suggestions.

Schema rules — this is where affiliate sites get penalized:
- `Product` schema only when the fields come from tier 1 or 4.
- `aggregateRating` / `review` **only** with genuine merchant-API rating data. Never generated. If absent, the property is absent.
- `FAQPage` schema only when the FAQ is substantive and on-page.
- `Offer` price only when fresh per §3; otherwise the offer omits price rather than lying.
- Affiliate articles are excluded from the Google News sitemap and included in the standard sitemap.

---

## 11. Analytics

Tracked: article views, CTA impressions, CTA clicks, CTR, clicks per product / merchant / article / campaign / button variant, and conversions where the network reports them (postback or CSV import — most networks do not offer real-time conversion APIs; the schema accepts both).

Dashboard: Top Products, Top Articles, Top Merchants, Top CTA Buttons, Best Converting Categories, plus revenue against AI cost per article so unit economics are visible.

Attribution honesty: we can measure clicks reliably. Conversions and revenue depend entirely on what each network reports, so those columns show "not reported" rather than an estimate.

---

## 12. Link health checker

Celery `maintain` task, every 6 h for active links, hourly for links on articles published in the last 48 h:

1. HEAD (falling back to a ranged GET) through the guarded client, following redirects.
2. Classify: `ok`, `redirected` (record new destination), `broken` (4xx/5xx), `expired` (redirects to a homepage or search page — a common silent failure), `timeout`.
3. Two consecutive failures → link `status='LINK_ERROR'`, product flagged, CTA hidden, admin notified.
4. Every check is recorded in `affiliate_link_health_checks` so flapping is visible.

Never fail silently, never keep sending readers to a dead link.

---

## 13. Statuses

**Product:** `ACTIVE`, `INACTIVE`, `NEEDS_METADATA`, `LINK_ERROR`, `EXPIRED`
**Affiliate article:** `DRAFT`, `GENERATING`, `QUALITY_CHECK`, `READY`, `SCHEDULED`, `PUBLISHED`, `FAILED`

`NEEDS_METADATA` is a first-class, expected state with an admin queue where a human can supply the missing fields in a form and resume the workflow with one click. This is the honest version of "one link → complete article": it works fully automatically when the data is obtainable, and asks for exactly what is missing when it is not.

---

## 14. Quality gate (affiliate-specific, in addition to the standard QA)

Hard failures: any banned experiential phrase; any currency figure with no tier-1/4 price field; any rating with no rating field; any spec not traceable to a metadata field; missing disclosure; missing `rel="sponsored"`; affiliate link in a news-type article; below the thin-content thresholds; a "best" ranking with no stated criteria; a broken destination link at publish time.

Every one of these has a corresponding test. The gate runs on the VPS in Python, so no model output can bypass it.

---

## 15. Admin section

`AFFILIATE MARKETING` → Add Affiliate Product · Products · Generated Articles · Affiliate Links · Campaigns · Click Analytics · Revenue Tracking · Disclosures · CTA Templates · **Needs Metadata** (the queue that makes the rest work).

**Add Product form:** Affiliate URL (required) · Product Name override · Brand · Category · Target Audience · Article Type (default *Auto*) · Primary Keyword · Publish Mode (Draft / Automatic / Schedule).

Paste-only is the intended path; everything else is an override.
