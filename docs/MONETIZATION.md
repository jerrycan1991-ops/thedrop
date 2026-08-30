# THE DROP — Monetization

Phase 1 ships **no ads and no paywall**. It ships the *abstractions* so that turning revenue on later is configuration, not a refactor.

Principle: revenue mechanics never touch editorial decisions. `monetization_fit` is capped at 5 % of the opportunity score and cannot move a story past a verification gate (PIPELINE.md §9). Ad density never determines article length or pagination.

---

## 1. Ad abstraction

Business logic never imports AdSense. Components render slots; a provider resolves them.

```tsx
<AdSlot placement="mid_article" category={article.category} riskTier={article.riskTier} />
```

Resolution order at render time:
1. Look up `ad_placements` for `slot_key`.
2. If `is_active=false`, or the article's `article_type`/`category` is excluded, or `risk_tier` is in `excluded_risk_tiers` → render nothing (not an empty box — nothing, so layout does not shift).
3. Otherwise dispatch to the configured provider component.

```ts
interface AdProvider {
  id: 'adsense' | 'direct' | 'house' | 'none';
  render(slot: SlotConfig): ReactNode;
  isEligible(ctx: AdContext): boolean;
}
```

Placements: `header`, `after_intro`, `mid_article`, `sidebar`, `article_end`, `home_module_1..n`.

**Brand-safety exclusions are default-on.** Stories with `risk_tier='high'` (deaths, crime, war, tragedy) render no ads. This protects both readers and the AdSense account — policy strikes on sensitive-content ad placement are the fastest way to lose the account.

Layout rule: every slot reserves its dimensions before load (`min-height` from the slot config) so ads cannot damage CLS. Core Web Vitals are a ranking input; an ad implementation that tanks CLS costs more traffic than it earns.

---

## 2. AdSense readiness

AdSense approval requires things that are Phase 1 and Phase 5 work, not launch-day work:

| Requirement | Where it's satisfied |
|---|---|
| Original, substantial content | The pipeline — never rewrites, always original from evidence packets |
| Clear site identity and purpose | `/about` |
| Privacy policy, terms, contact | `/privacy`, `/terms`, `/contact` |
| Editorial standards and corrections | `/editorial-policy`, `/corrections` |
| Navigable structure, working links | IA in Phase 1 |
| Sufficient content volume and history | ~30 days of publishing before applying |
| No prohibited content | Category and risk rules |

**Recommendation: do not apply until ~30 days and ~400 articles of live, healthy publishing.** Applying early with a thin site risks a rejection that is slower to recover from than simply waiting.

AI-content disclosure: `/about` and `/editorial-policy` state plainly that articles are AI-assisted and human-governed, and describe the verification process. This is honest, and it is also what Google's guidance rewards — content quality matters, undisclosed deception does not.

---

## 3. Revenue streams and sequencing

| Stream | Phase | Notes |
|---|---|---|
| AdSense display | 5 (apply), live after approval | primary early revenue |
| Direct/premium display | later | requires traffic; `direct` provider already in the abstraction |
| Affiliate | 5 | strictly limited, see §4 |
| Newsletter sponsorship | 6+ | needs list size |
| Subscriptions (PREMIUM) | post-Phase 8 | schema ready now, not implemented |
| Sponsored content | later | labeled, `is_sponsored`, never in `NEWS` |
| Syndication/licensing | opportunistic | original content is licensable |

---

## 4. Affiliate rules

The full affiliate content automation engine is designed in **`docs/AFFILIATE_ENGINE.md`** (Phase 5B) with its data model in DATABASE.md §14 and its anti-fabrication contract in ADR-0009. The rules below are the editorial boundary it operates inside:

- **Never in a `NEWS` article.** Affiliate links belong in explicitly commerce-oriented content (deals, product explainers, buying guides) — a separate article type.
- Disclosure is rendered above the first affiliate link, not buried in a footer.
- `rel="sponsored nofollow"` on every affiliate link, enforced by the renderer, not by the author.
- No affiliate link may be inserted post-publication by an automated process into an article that did not have one.
- Click tracking is first-party (`/go/{id}` → 302), so we own the data and can measure honestly.

A test asserts that an article with `article_type='NEWS'` and any affiliate link fails QA.

---

## 5. Subscriptions (architecture only)

`users.subscription_tier ∈ {FREE, REGISTERED, PREMIUM}` exists from Phase 1. Nothing gates on it.

Planned when the time comes: metered access (N free articles/month, cookie-based), ad-free for PREMIUM, early access and newsletter extras. Payment provider (Stripe) integrates behind a `PaymentProvider` interface. **No paywall in Phase 1** — a new publication with no audience monetizes attention, not scarcity.

Note for later: metered paywalls interact with Google News/Discover eligibility. Structured data must declare `isAccessibleForFree` and `hasPart` correctly, or indexing suffers. That is a Phase-9 design task, flagged now so it is not discovered late.

---

## 6. Newsletter

Provider abstraction from Phase 5:

```python
class NewsletterProvider(Protocol):
    def subscribe(self, email: str, prefs: dict) -> SubscribeResult: ...
    def unsubscribe(self, email: str) -> None: ...
    def send_campaign(self, campaign: Campaign) -> SendResult: ...
```

Phase 1 stores subscribers in our own `newsletter_subscribers` table with double opt-in — so the list is **ours**, portable, and provider-independent from day one. Sending is added in Phase 5 with a hosted ESP behind the interface. This ordering matters: a list locked inside a vendor is a liability.

Daily digest and breaking-news alerts are the two planned campaign types.

---

## 7. Cost side of the ledger

Revenue is meaningless without unit economics, so the same dashboard shows both. Tracked per article: Claude tokens and cost by purpose, image GPU-seconds, video GPU-seconds, provider API calls attributable to the story, and total cost-per-published-article.

Budgets (`budgets` table) support daily, monthly, per-category and per-job-type limits with `warn` / `throttle` / `halt` behaviour on breach, plus a global emergency AI disable.

**Model routing is the main cost lever**, and it is a config table, not code:

| Task | Tier | Why |
|---|---|---|
| Classification, entity/claim extraction, scoring | Haiku | high volume, structured, cheap |
| Article generation, headlines, QA | Sonnet | quality where it shows |
| High-risk verification, second review | Opus | where being wrong is expensive |

Prompt caching is used for the system prompt and house style block, which are identical across every generation in a category — that is the single largest available saving.

Per-token rates are **not written into this document or into code**; they are seeded into `model_pricing` from configuration and must be filled from the current Anthropic pricing page before cost gates are enabled. A cost model built on invented numbers is worse than none.

Break-even framing: revenue is roughly `daily_pageviews × RPM / 1000`; cost is roughly `articles_per_day × cost_per_article + VPS`. The dashboard reports both against each other daily so the answer is measured, not assumed.

---

## 8. What we will not do

- No clickbait headlines for CTR (hard filter in the headline scorer).
- No article splitting across pages to multiply impressions.
- No auto-refreshing ads, no interstitials on article pages, no pop-ups over content.
- No ads on high-risk/tragedy stories.
- No selling reader data. First-party analytics only.
- No undisclosed sponsored content.

These are enforced in code and tests where possible, because a policy that lives only in a document erodes.
