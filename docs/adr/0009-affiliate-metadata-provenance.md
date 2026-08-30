# ADR-0009: Affiliate product data carries per-field provenance; adapters are network-agnostic

Status: Accepted (Phase 0)
Date: 2026-08-30

## Context

The affiliate engine must turn a pasted URL into a complete article automatically, while never inventing a price, specification, rating, review or availability claim. Those two goals collide precisely where product metadata is unreliable — which is most of the time. Merchant pages are frequently unfetchable (Amazon's terms require the Product Advertising API; many large retailers block automated requests), and structured data on the pages that *are* fetchable ranges from complete to absent.

A conventional implementation stores a product row with nullable columns and lets the generator write around the gaps. That reliably produces fabricated specs, because a model handed an incomplete product and asked for a review will fill the holes.

## Decision

1. **Every product field is a `Field[T]` carrying `value`, `source`, `confidence` and `fetched_at`** — not a bare column. Provenance is part of the type, so it cannot be dropped by accident.
2. **A four-tier extraction ladder** with recorded provenance: official network API → `schema.org/Product` JSON-LD → OpenGraph/meta → admin manual entry. Failure is an explicit `NEEDS_METADATA` state, never a guess.
3. **Rendering rules are keyed on provenance**, not on presence. Prices and availability render only from tier 1 or 4 and only within a freshness window; ratings render only from tier 1; anything else is omitted or hedged in language.
4. **All merchant/network access goes through an `AffiliateNetworkAdapter`.** No network name appears in pipeline code. Adapters whose terms forbid page fetching expose only the API and manual tiers.
5. **The publish gate re-checks traceability in Python**: a currency figure with no tier-1/4 price field, or a rating with no rating field, is a hard failure regardless of how good the article reads.

## Rationale

- Making provenance a type rather than a convention means the anti-fabrication rule is enforced by the compiler and the gate, not by prompt wording. Prompt-level rules degrade; type-level rules do not.
- `NEEDS_METADATA` as a first-class state with an admin queue preserves the one-click experience where data is obtainable, and asks for exactly the missing fields where it is not. That is the honest version of full automation.
- The adapter boundary means adding Impact, CJ, ShareASale, Rakuten, Walmart, Best Buy or Amazon PA-API later is a self-contained module and a credential — no pipeline change, no schema change.
- Rating and price fabrication are the two failures that draw FTC attention and Google spam action. Structurally preventing them protects the whole domain, not just the commerce section.

## Consequences

- More storage and more code than plain columns. Accepted: this is the safety mechanism.
- Some pasted links will not produce an article without human input. That is the correct outcome, and the admin queue makes it a 30-second task rather than a dead end.
- Comparison tables and schema markup are built from stored fields deterministically, so the model cannot author a table cell or a star rating.
- Phase 5B ships with `generic` and `manual` adapters only. The system does not claim API access it does not have.
