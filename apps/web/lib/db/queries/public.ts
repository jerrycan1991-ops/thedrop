import "server-only";

import { query, queryOne } from "@/lib/db/client";

/**
 * Read queries backing the public API.
 *
 * These are transliterations of the SQLAlchemy queries in
 * `services/api/app/routers/public.py`. Both implementations run side by side until
 * the Python routes are retired, and `api_baseline.py compare` proves they agree.
 *
 * Parameterized only — no string interpolation anywhere, same rule as the Python side.
 */

/* -------------------------------------------------------------------------- */
/* Row shapes                                                                  */
/* -------------------------------------------------------------------------- */

interface CategoryRow {
  slug: string;
  name: string;
  description: string | null;
  accent_token: string;
  is_commercial: boolean;
}

interface ArticleSummaryRow {
  public_id: string;
  slug: string;
  headline: string;
  dek: string;
  article_type: string;
  published_at: Date | null;
  updated_at_public: Date | null;
  first_published_at: Date | null;
  reading_time_seconds: number;
  is_sponsored: boolean;
  category_slug: string;
  category_name: string;
  category_description: string | null;
  category_accent_token: string;
  media_public_id: string | null;
  media_storage_key: string | null;
  media_width: number | null;
  media_height: number | null;
  media_alt_text: string | null;
  media_caption: string | null;
  media_credit: string | null;
  media_blurhash: string | null;
  media_rights_status: string | null;
  media_is_ai_generated: boolean | null;
  media_ai_disclosure_text: string | null;
}

interface ArticleDetailRow extends ArticleSummaryRow {
  body_blocks: unknown[];
  key_facts: string[];
  byline: string;
  structured_data: Record<string, unknown>;
  seo_title: string;
  meta_description: string;
  og_title: string;
  og_description: string;
  canonical_url: string | null;
  noindex: boolean;
  disclosure_text: string | null;
}

/* -------------------------------------------------------------------------- */
/* Serialization — mirrors _serialize_summary() in public.py                    */
/* -------------------------------------------------------------------------- */

/**
 * The public URL path, derived not stored — identical to `Article.path` in Python.
 * Month and day are zero-padded; an unpublished article has no public path.
 */
function articlePath(row: ArticleSummaryRow): string {
  if (row.first_published_at === null) {
    return `/preview/${row.public_id}`;
  }
  const d = row.first_published_at;
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return `/${row.category_slug}/${yyyy}/${mm}/${dd}/${row.slug}`;
}

/**
 * Python emits `datetime.isoformat()`, which yields `+00:00` for a UTC-aware value —
 * NOT JavaScript's default `Z`. Matching this exactly matters: the baseline compares
 * the string.
 */
function isoUtc(value: Date | null): string | null {
  if (value === null) return null;
  return value.toISOString().replace(/\.(\d{3})Z$/, ".$1000+00:00");
}

function serializeSummary(row: ArticleSummaryRow): Record<string, unknown> {
  return {
    id: row.public_id,
    slug: row.slug,
    path: articlePath(row),
    headline: row.headline,
    dek: row.dek,
    articleType: row.article_type,
    category: {
      slug: row.category_slug,
      name: row.category_name,
      description: row.category_description,
      accentToken: row.category_accent_token,
    },
    publishedAt: isoUtc(row.published_at),
    updatedAt: isoUtc(row.updated_at_public),
    readingTimeSeconds: row.reading_time_seconds,
    isSponsored: row.is_sponsored,
    heroImage:
      row.media_public_id === null
        ? null
        : {
            id: row.media_public_id,
            url: `/media/${row.media_storage_key}`,
            width: row.media_width,
            height: row.media_height,
            altText: row.media_alt_text,
            caption: row.media_caption,
            credit: row.media_credit,
            blurhash: row.media_blurhash,
            rightsStatus: row.media_rights_status,
            isAiGenerated: row.media_is_ai_generated,
            aiDisclosure: row.media_ai_disclosure_text,
          },
  };
}

/* -------------------------------------------------------------------------- */
/* SQL                                                                         */
/* -------------------------------------------------------------------------- */

// Only published, non-deleted articles are ever visible publicly.
const PUBLISHED_SELECT = `
  SELECT
    a.public_id::text        AS public_id,
    a.slug,
    a.headline,
    a.dek,
    a.article_type,
    a.published_at,
    a.updated_at_public,
    a.first_published_at,
    a.reading_time_seconds,
    a.is_sponsored,
    c.slug                   AS category_slug,
    c.name                   AS category_name,
    c.description            AS category_description,
    c.accent_token           AS category_accent_token,
    m.public_id::text        AS media_public_id,
    m.storage_key            AS media_storage_key,
    m.width                  AS media_width,
    m.height                 AS media_height,
    m.alt_text               AS media_alt_text,
    m.caption                AS media_caption,
    m.credit                 AS media_credit,
    m.blurhash               AS media_blurhash,
    m.rights_status          AS media_rights_status,
    m.is_ai_generated        AS media_is_ai_generated,
    m.ai_disclosure_text     AS media_ai_disclosure_text
  FROM articles a
  JOIN categories c    ON c.id = a.category_id
  LEFT JOIN media_assets m ON m.id = a.hero_media_id
  WHERE a.status = 'published' AND a.deleted_at IS NULL
`;

export async function listCategories(): Promise<Record<string, unknown>[]> {
  const rows = await query<CategoryRow>(
    `SELECT slug, name, description, accent_token, is_commercial
       FROM categories
      WHERE is_active = true
      ORDER BY sort_order`,
  );
  return rows.map((row) => ({
    slug: row.slug,
    name: row.name,
    description: row.description,
    accentToken: row.accent_token,
    isCommercial: row.is_commercial,
  }));
}

export interface ArticleListResult {
  items: Record<string, unknown>[];
  page: number;
  pageSize: number;
  hasMore: boolean;
  total: number;
}

export async function listArticles(options: {
  category: string | null;
  page: number;
  pageSize: number;
}): Promise<ArticleListResult> {
  const { category, page, pageSize } = options;
  const offset = (page - 1) * pageSize;

  // Fetch one extra row to determine hasMore without a second COUNT query. `total` is
  // therefore an ESTIMATE, matching the Python contract exactly — a true count here
  // would be a behaviour change, not an improvement.
  const limit = pageSize + 1;

  const rows = category
    ? await query<ArticleSummaryRow>(
        `${PUBLISHED_SELECT} AND c.slug = $1
         ORDER BY a.published_at DESC
         OFFSET $2 LIMIT $3`,
        [category, offset, limit],
      )
    : await query<ArticleSummaryRow>(
        `${PUBLISHED_SELECT}
         ORDER BY a.published_at DESC
         OFFSET $1 LIMIT $2`,
        [offset, limit],
      );

  const hasMore = rows.length > pageSize;
  const items = rows.slice(0, pageSize);

  return {
    items: items.map(serializeSummary),
    page,
    pageSize,
    hasMore,
    total: offset + items.length + (hasMore ? 1 : 0),
  };
}

export async function listLatest(limit: number): Promise<Record<string, unknown>[]> {
  const rows = await query<ArticleSummaryRow>(
    `${PUBLISHED_SELECT} ORDER BY a.published_at DESC LIMIT $1`,
    [limit],
  );
  return rows.map(serializeSummary);
}

export async function getArticleBySlug(
  categorySlug: string,
  slug: string,
): Promise<Record<string, unknown> | null> {
  const row = await queryOne<ArticleDetailRow>(
    `SELECT
       a.body_blocks, a.key_facts, a.byline, a.structured_data,
       a.seo_title, a.meta_description, a.og_title, a.og_description,
       a.canonical_url, a.noindex, a.disclosure_text,
       sub.*
     FROM (${PUBLISHED_SELECT} AND c.slug = $1 AND a.slug = $2) sub
     JOIN articles a ON a.public_id::text = sub.public_id`,
    [categorySlug, slug],
  );

  if (row === null) return null;

  const [sources, corrections, tags] = await Promise.all([
    query<{ publisher: string; title: string; url: string; ref_type: string }>(
      `SELECT r.publisher, r.title, r.url, r.ref_type
         FROM article_source_refs r
         JOIN articles a ON a.id = r.article_id
        WHERE a.public_id::text = $1
        ORDER BY r.display_order`,
      [row.public_id],
    ),
    query<{ correction_type: string; summary: string; detail: string; issued_at: Date }>(
      `SELECT co.correction_type, co.summary, co.detail, co.issued_at
         FROM corrections co
         JOIN articles a ON a.id = co.article_id
        WHERE a.public_id::text = $1 AND co.is_public = true`,
      [row.public_id],
    ),
    query<{ slug: string; name: string }>(
      `SELECT t.slug, t.name
         FROM tags t
         JOIN article_tags at ON at.tag_id = t.id
         JOIN articles a ON a.id = at.article_id
        WHERE a.public_id::text = $1`,
      [row.public_id],
    ),
  ]);

  return {
    ...serializeSummary(row),
    body: row.body_blocks,
    keyFacts: row.key_facts,
    byline: row.byline,
    tags: tags.map((t) => ({ slug: t.slug, name: t.name })),
    sources: sources.map((s) => ({
      publisher: s.publisher,
      title: s.title,
      url: s.url,
      refType: s.ref_type,
    })),
    corrections: corrections.map((c) => ({
      type: c.correction_type,
      summary: c.summary,
      detail: c.detail,
      issuedAt: isoUtc(c.issued_at),
    })),
    seo: {
      // Python falls back to the headline/dek when the SEO field is blank.
      title: row.seo_title || row.headline,
      metaDescription: row.meta_description || row.dek,
      ogTitle: row.og_title || row.headline,
      ogDescription: row.og_description || row.dek,
      canonicalUrl: row.canonical_url || articlePath(row),
      noindex: row.noindex,
    },
    structuredData: row.structured_data,
    disclosure: row.disclosure_text,
  };
}

/** Exported for unit tests that pin the pagination contract. */
export const __testing = { articlePath, isoUtc, serializeSummary };
