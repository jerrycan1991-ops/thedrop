/**
 * Shared, non-secret configuration for the web tier.
 *
 * Anything in here ships to the browser. Secrets, thresholds and gates live in the
 * Python settings module and the database — never here.
 */

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

/**
 * Categories are seeded in the database and are the source of truth at runtime.
 * This list exists so navigation and routing can be statically typed and so the
 * site renders sensibly before the database is reachable.
 *
 * Adding a category in production is a database row, not a code change.
 */
export const CATEGORIES = [
  { slug: "trending", name: "Trending", accentToken: "--cat-trending", order: 1 },
  { slug: "politics", name: "Politics", accentToken: "--cat-politics", order: 2 },
  { slug: "entertainment", name: "Entertainment", accentToken: "--cat-entertainment", order: 3 },
  { slug: "sports", name: "Sports", accentToken: "--cat-sports", order: 4 },
  { slug: "business", name: "Business", accentToken: "--cat-business", order: 5 },
  { slug: "technology", name: "Technology", accentToken: "--cat-technology", order: 6 },
  { slug: "world", name: "World", accentToken: "--cat-world", order: 7 },
] as const;

export type CategorySlug = (typeof CATEGORIES)[number]["slug"];

export const CATEGORY_SLUGS: readonly string[] = CATEGORIES.map((c) => c.slug);

export const PRIMARY_NAV = [
  ...CATEGORIES.map((c) => ({ href: `/${c.slug}`, label: c.name })),
  { href: "/latest", label: "Latest" },
] as const;

export const FOOTER_NAV = [
  { href: "/about", label: "About" },
  { href: "/contact", label: "Contact" },
  { href: "/editorial-policy", label: "Editorial Policy" },
  { href: "/corrections", label: "Corrections" },
  { href: "/affiliate-disclosure", label: "Affiliate Disclosure" },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
] as const;

/**
 * Article types. The label is always rendered — the discipline of distinguishing
 * news from opinion is built into the template, not bolted on later.
 */
export const ARTICLE_TYPES = {
  NEWS: { label: "News", tone: "neutral" },
  ANALYSIS: { label: "Analysis", tone: "info" },
  OPINION: { label: "Opinion", tone: "accent" },
  COMMENTARY: { label: "Commentary", tone: "accent" },
  BREAKING: { label: "Breaking", tone: "breaking" },
  EXPLAINER: { label: "Explainer", tone: "info" },
  LIVE: { label: "Live", tone: "breaking" },
} as const;

export type ArticleType = keyof typeof ARTICLE_TYPES;

/** Editorial article types may never carry an affiliate link. Enforced in the DB too. */
export const EDITORIAL_ARTICLE_TYPES: readonly ArticleType[] = [
  "NEWS",
  "ANALYSIS",
  "OPINION",
  "COMMENTARY",
];

/** Commercial types live under /picks and are excluded from the Google News sitemap. */
export const COMMERCIAL_ARTICLE_TYPES = {
  PRODUCT_REVIEW: { label: "Product Review" },
  BUYING_GUIDE: { label: "Buying Guide" },
  BEST_PRODUCTS_LIST: { label: "Best Products" },
  PRODUCT_COMPARISON: { label: "Comparison" },
  PRODUCT_ROUNDUP: { label: "Roundup" },
  GIFT_GUIDE: { label: "Gift Guide" },
  BEST_FOR_GUIDE: { label: "Best For" },
  TRENDING_PRODUCT: { label: "Trending" },
  NEWS_PLUS_RECOMMENDATION: { label: "News + Picks" },
  HOW_TO: { label: "How-To" },
  DEALS: { label: "Deals" },
} as const;

export type CommercialArticleType = keyof typeof COMMERCIAL_ARTICLE_TYPES;

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
