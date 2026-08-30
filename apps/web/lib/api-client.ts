import "server-only";

import { SITE } from "@thedrop/config";

import { DatabaseUnavailableError } from "@/lib/db/client";
import {
  getArticleBySlug,
  listArticles,
  listCategories,
  listLatest,
} from "@/lib/db/queries/public";

/**
 * Server-side data access for public pages.
 *
 * PHASE 2: these functions used to `fetch()` the FastAPI service. They now call the
 * query layer directly, which removes an HTTP hop from every uncached render — the
 * whole point of the migration. On split hosting that hop crossed the public internet
 * on every ISR revalidation, and TTFB feeds Core Web Vitals, which feeds Google News.
 *
 * The exported signatures are unchanged, so no page component needed editing.
 *
 * Caching now comes from the page's own `export const revalidate`, not from `fetch`
 * cache tags. That is the layer that actually matters: a cached page does no work at
 * all, rather than doing cached work.
 *
 * The public HTTP endpoints still exist as route handlers under `app/api/v1/public/`
 * for external consumers; both paths share this same query layer, so they cannot drift.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * The site must stay up when the database is not.
 *
 * A category rail that cannot load is an empty rail, not a 500 for the whole page.
 * Genuine "not found" is different and is signalled by ApiError(404), which the
 * article page turns into `notFound()`.
 */
async function safe<T>(label: string, fallback: T, fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof DatabaseUnavailableError) {
      console.error(`[data] ${label}: database unavailable`);
    } else {
      console.error(`[data] ${label} failed`, error);
    }
    return fallback;
  }
}

export interface ApiCategory {
  slug: string;
  name: string;
  description: string | null;
  accentToken: string;
  isCommercial: boolean;
}

export interface ApiArticleSummary {
  id: string;
  slug: string;
  path: string;
  headline: string;
  dek: string;
  articleType: string;
  category: { slug: string; name: string; description: string | null; accentToken: string };
  publishedAt: string | null;
  updatedAt: string | null;
  readingTimeSeconds: number;
  isSponsored: boolean;
  heroImage: {
    id: string;
    url: string;
    width: number;
    height: number;
    altText: string;
    caption: string | null;
    credit: string | null;
    blurhash: string | null;
    rightsStatus: string;
    isAiGenerated: boolean;
    aiDisclosure: string | null;
  } | null;
}

export interface ApiArticle extends ApiArticleSummary {
  body: unknown[];
  keyFacts: string[];
  byline: string;
  tags: { slug: string; name: string }[];
  sources: { publisher: string; title: string; url: string; refType: string }[];
  corrections: { type: string; summary: string; detail: string; issuedAt: string }[];
  seo: {
    title: string;
    metaDescription: string;
    ogTitle: string;
    ogDescription: string;
    canonicalUrl: string;
    noindex: boolean;
  };
  structuredData: Record<string, unknown>;
  disclosure: string | null;
}

interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}

export function getCategories(): Promise<ApiCategory[]> {
  return safe("categories", [], async () => (await listCategories()) as unknown as ApiCategory[]);
}

export function getArticles(
  params: { category?: string; page?: number; pageSize?: number } = {},
): Promise<Paged<ApiArticleSummary>> {
  const page = params.page ?? 1;
  const pageSize = params.pageSize ?? 20;

  return safe(
    "articles",
    { items: [], total: 0, page, pageSize, hasMore: false },
    async () =>
      (await listArticles({
        category: params.category ?? null,
        page,
        pageSize,
      })) as unknown as Paged<ApiArticleSummary>,
  );
}

export function getLatest(limit = 20): Promise<{ items: ApiArticleSummary[] }> {
  return safe("latest", { items: [] }, async () => ({
    items: (await listLatest(limit)) as unknown as ApiArticleSummary[],
  }));
}

/**
 * Throws ApiError(404) when the article does not exist or the date path does not
 * match — the caller turns that into `notFound()`. Unlike the list endpoints this
 * does NOT swallow failures: rendering an empty article page would be worse than an
 * error.
 */
export async function getArticle(
  category: string,
  year: string,
  month: string,
  day: string,
  slug: string,
): Promise<ApiArticle> {
  const article = (await getArticleBySlug(category, slug)) as unknown as ApiArticle | null;

  if (article === null) {
    throw new ApiError(404, "Article not found");
  }

  // The date is part of the canonical URL; the same article served at two paths would
  // split its ranking signals.
  const expected = `/${category}/${year.padStart(4, "0")}/${month.padStart(2, "0")}/${day.padStart(2, "0")}/${slug}`;
  if (article.path !== expected) {
    throw new ApiError(404, "Article not found");
  }

  return article;
}

export const siteUrl = SITE.url;
