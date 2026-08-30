/**
 * Types shared between the web app and the FastAPI contract.
 *
 * These mirror the Pydantic response schemas in `services/api`. They are hand-kept
 * rather than generated in Phase 1; a schema-generation step is a Phase 2 task once
 * the API surface stops moving. Any drift is caught by the API contract tests.
 */

import type { ArticleType, CategorySlug, CommercialArticleType } from "@thedrop/config";

export type Iso8601 = string;
export type Uuid = string;

export interface Category {
  slug: CategorySlug | string;
  name: string;
  description: string | null;
  accentToken: string;
}

export interface Tag {
  slug: string;
  name: string;
}

export interface MediaAsset {
  id: Uuid;
  url: string;
  width: number;
  height: number;
  altText: string;
  caption: string | null;
  credit: string | null;
  blurhash: string | null;
  /** Only these statuses may appear on a published page. */
  rightsStatus: "ORIGINAL_AI" | "LICENSED" | "PUBLIC_DOMAIN" | "VALIDATED_CC";
  isAiGenerated: boolean;
  /** Rendered as a visible label whenever `isAiGenerated` is true. */
  aiDisclosure: string | null;
}

export interface SourceRef {
  publisher: string;
  title: string;
  url: string;
  refType: "reporting" | "primary_document" | "data" | "quote";
}

export interface ArticleSummary {
  id: Uuid;
  slug: string;
  path: string;
  headline: string;
  dek: string;
  articleType: ArticleType | CommercialArticleType;
  category: Category;
  publishedAt: Iso8601 | null;
  updatedAt: Iso8601 | null;
  readingTimeSeconds: number;
  heroImage: MediaAsset | null;
  isSponsored: boolean;
}

export interface Correction {
  type: "correction" | "clarification" | "update" | "retraction";
  summary: string;
  detail: string;
  issuedAt: Iso8601;
}

export interface Article extends ArticleSummary {
  body: ArticleBlock[];
  keyFacts: string[];
  byline: string;
  sources: SourceRef[];
  corrections: Correction[];
  tags: Tag[];
  seo: {
    title: string;
    metaDescription: string;
    ogTitle: string;
    ogDescription: string;
    canonicalUrl: string;
    noindex: boolean;
  };
  /** JSON-LD, generated server-side. Never hand-authored. */
  structuredData: Record<string, unknown>;
  disclosure: string | null;
}

/**
 * Body is a block list rather than raw HTML so ad slots and affiliate CTAs can be
 * placed between blocks by rule, and so the renderer never needs
 * `dangerouslySetInnerHTML` on model output.
 */
export type ArticleBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; level: 2 | 3; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; text: string; attribution: string | null; sourceUrl: string | null }
  | { kind: "image"; asset: MediaAsset }
  | { kind: "keyFacts"; items: string[] }
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "affiliateCta"; ctaId: Uuid }
  | { kind: "adSlot"; placement: string };

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}

export interface HealthResponse {
  status: "ok" | "degraded" | "error";
  version: string;
  environment: string;
}

export interface ReadyResponse extends HealthResponse {
  database: boolean;
  redis: boolean;
  migrations: "head" | "behind" | "unknown";
}

/** Desktop AI worker status, surfaced on the admin System Health page. */
export interface WorkerStatus {
  name: string;
  status: "online" | "degraded" | "offline";
  lastHeartbeatAt: Iso8601 | null;
  currentJobCount: number;
  gpuName: string | null;
  queueDepth: number;
  oldestQueuedJobAgeSeconds: number | null;
}
