import type { NextRequest } from "next/server";

import {
  type FieldError,
  handleRoute,
  notFound,
  ok,
  requestIdFrom,
  validationFailed,
} from "@/lib/api/contract";
import { getArticleBySlug } from "@/lib/db/queries/public";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{
    category: string;
    year: string;
    month: string;
    day: string;
    slug: string;
  }>;
}

/** FastAPI declares year/month/day as `int` path params; a non-integer is a 422. */
function parsePathInt(name: string, raw: string, errors: FieldError[]): number | null {
  if (!/^[+-]?\d+$/.test(raw)) {
    errors.push({
      field: `path.${name}`,
      message: "Input should be a valid integer, unable to parse string as an integer",
    });
    return null;
  }
  return Number.parseInt(raw, 10);
}

/**
 * Mirrors `get_article` in services/api/app/routers/public.py.
 *
 * The date is part of the canonical URL, so an article reached at the wrong date is a
 * 404 rather than a redirect — serving one article at two paths creates duplicates
 * that split ranking signals.
 */
export async function GET(request: NextRequest, context: RouteContext) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const { category, year, month, day, slug } = await context.params;

    const errors: FieldError[] = [];
    const y = parsePathInt("year", year, errors);
    const m = parsePathInt("month", month, errors);
    const d = parsePathInt("day", day, errors);
    if (errors.length > 0) return validationFailed(errors, requestId);

    const article = await getArticleBySlug(category, slug);
    if (article === null) return notFound("Article not found", requestId);

    const publishedIso = article.path as string;
    // `path` is derived from first_published_at, so comparing against it is the same
    // check Python makes against the (year, month, day) tuple — without a second
    // round trip for the raw timestamp.
    const expected = `/${category}/${String(y).padStart(4, "0")}/${String(m).padStart(2, "0")}/${String(d).padStart(2, "0")}/${slug}`;
    if (publishedIso !== expected) return notFound("Article not found", requestId);

    return ok(article, requestId);
  });
}
