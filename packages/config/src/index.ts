/**
 * Shared, non-secret configuration for the web tier.
 *
 * Anything in here ships to the browser. Secrets, thresholds and gates live in the
 * Python settings module and the database — never here.
 *
 * Two deliberately different source-of-truth strategies (see docs/DOMAIN_MODEL.md):
 *
 *   Categories    — runtime data. The `categories` TABLE is authoritative. There is
 *                   no category list in this file; the web app reads them from the
 *                   server layer via `apps/web/lib/categories.ts`.
 *
 *   Article types — a closed set that business logic switches on. Canonical
 *                   definition is `article_types.json`, imported below and read by
 *                   Python from the same file. Neither language re-declares it.
 */

import articleTypeDefinition from "./thedrop_config/article_types.json";

export const SITE = {
  name: "The Drop",
  wordmark: "THE DROP",
  domain: "thedrop.channel",
  url: process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3100",
  tagline: "Fast, verified, US-first news.",
  description:
    "Verified US news, analysis and culture. Original reporting built from primary sources and cross-checked evidence.",
  locale: "en_US",
  twitterHandle: "@thedrop",
} as const;

/* -------------------------------------------------------------------------- */
/* Article types — derived from the canonical JSON, never hand-listed          */
/* -------------------------------------------------------------------------- */

/**
 * Editorial article types. The label is always rendered: distinguishing news from
 * analysis from opinion is an editorial obligation, not a display preference.
 */
export const ARTICLE_TYPES = articleTypeDefinition.editorial;

export type ArticleType = keyof typeof ARTICLE_TYPES;

/** Commercial formats. Live under /picks, excluded from the Google News sitemap. */
export const COMMERCIAL_ARTICLE_TYPES = articleTypeDefinition.commercial;

export type CommercialArticleType = keyof typeof COMMERCIAL_ARTICLE_TYPES;

/**
 * The editorial types that may never carry an affiliate link, CTA or product
 * placement. Derived from the same `forbidsCommercial` flag that generates the
 * database CHECK constraint, so the UI and the schema cannot disagree.
 */
export const EDITORIAL_ARTICLE_TYPES: readonly ArticleType[] = (
  Object.entries(ARTICLE_TYPES) as [ArticleType, { forbidsCommercial: boolean }][]
)
  .filter(([, spec]) => spec.forbidsCommercial)
  .map(([name]) => name);

export function isEditorialArticleType(value: string): boolean {
  return (EDITORIAL_ARTICLE_TYPES as readonly string[]).includes(value);
}

export function isKnownArticleType(value: string): boolean {
  return value in ARTICLE_TYPES || value in COMMERCIAL_ARTICLE_TYPES;
}

/* -------------------------------------------------------------------------- */
/* Navigation                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Links that are NOT categories. Category links are appended at runtime from the
 * database — see `getPrimaryNav()` in apps/web/lib/categories.ts.
 */
export const STATIC_PRIMARY_NAV = [{ href: "/latest", label: "Latest" }] as const;

export const FOOTER_NAV = [
  { href: "/about", label: "About" },
  { href: "/contact", label: "Contact" },
  { href: "/editorial-policy", label: "Editorial Policy" },
  { href: "/corrections", label: "Corrections" },
  { href: "/affiliate-disclosure", label: "Affiliate Disclosure" },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
] as const;

/* -------------------------------------------------------------------------- */
/* Placements                                                                  */
/* -------------------------------------------------------------------------- */

export const AD_PLACEMENTS = [
  "header",
  "after_intro",
  "mid_article",
  "sidebar",
  "article_end",
  "home_module",
] as const;

export type AdPlacement = (typeof AD_PLACEMENTS)[number];

export const AFFILIATE_CTA_PLACEMENTS = [
  "after_intro",
  "after_overview",
  "after_features",
  "before_verdict",
  "article_end",
  "product_card",
] as const;

export type AffiliateCtaPlacement = (typeof AFFILIATE_CTA_PLACEMENTS)[number];
