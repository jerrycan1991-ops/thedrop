import "server-only";

import { SITE } from "@thedrop/config";

/**
 * Server-side client for the FastAPI service.
 *
 * The web app never opens a database connection (ADR-0006). Everything it renders
 * comes through here, which is where caching and revalidation are controlled.
 *
 * Requests go direct to the internal URL rather than through the public origin —
 * one fewer hop, and it works during build when no public hostname is listening.
 */
const API_BASE = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface FetchOptions {
  /** ISR window in seconds. `false` disables caching (admin reads). */
  revalidate?: number | false;
  tags?: string[];
  headers?: Record<string, string>;
}

async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { revalidate = 60, tags, headers } = options;

  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json", ...headers },
    next: revalidate === false ? { revalidate: 0 } : { revalidate, tags },
  });

  if (!response.ok) {
    throw new ApiError(response.status, `GET ${path} failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

/**
 * Read that tolerates the API being unavailable.
 *
 * The site must stay up when the backend is not. A category rail that cannot load is
 * an empty rail, not a 500 for the whole page — so callers get a fallback rather than
 * an exception. Genuine 404s are handled by the caller via `notFound()`.
 */
export async function safeFetch<T>(
  path: string,
  fallback: T,
  options: FetchOptions = {},
): Promise<T> {
  try {
    return await apiFetch<T>(path, options);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw error;
    console.error(`[api] ${path} unavailable`, error);
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
  return safeFetch<ApiCategory[]>("/api/v1/public/categories", [], { revalidate: 300 });
}

export function getArticles(params: {
  category?: string;
  page?: number;
  pageSize?: number;
} = {}): Promise<Paged<ApiArticleSummary>> {
  const query = new URLSearchParams();
  if (params.category) query.set("category", params.category);
  if (params.page) query.set("page", String(params.page));
  if (params.pageSize) query.set("page_size", String(params.pageSize));

  const suffix = query.toString() ? `?${query}` : "";
  return safeFetch(`/api/v1/public/articles${suffix}`, {
    items: [],
    total: 0,
    page: params.page ?? 1,
    pageSize: params.pageSize ?? 20,
    hasMore: false,
  });
}

export function getLatest(limit = 20): Promise<{ items: ApiArticleSummary[] }> {
  return safeFetch(`/api/v1/public/latest?limit=${limit}`, { items: [] }, { revalidate: 30 });
}

export function getArticle(
  category: string,
  year: string,
  month: string,
  day: string,
  slug: string,
): Promise<ApiArticle> {
  return apiFetch<ApiArticle>(
    `/api/v1/public/articles/${category}/${year}/${month}/${day}/${slug}`,
    { revalidate: 120, tags: [`article:${slug}`] },
  );
}

export const siteUrl = SITE.url;
