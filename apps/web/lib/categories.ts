import "server-only";

import { cache } from "react";

import { STATIC_PRIMARY_NAV } from "@thedrop/config";

import { getCategories, type ApiCategory } from "@/lib/api-client";

/**
 * Categories come from the database. There is no hardcoded category list anywhere in
 * the TypeScript codebase — adding a category is a row, not a deploy.
 *
 * `cache()` deduplicates within a single render pass, so a page that shows categories
 * in the header, the footer and a module fetches them once. Across requests, the
 * underlying fetch is ISR-cached for 300s by the API client.
 */
export const getAllCategories = cache(async (): Promise<ApiCategory[]> => {
  return getCategories();
});

/**
 * Categories shown in site navigation.
 *
 * Commercial sections (`/picks`) are excluded: they are reachable from articles and
 * the footer, but promoting affiliate content to the top-level news nav is exactly
 * the blurring of editorial and commercial that the whole design avoids.
 */
export const getNavCategories = cache(async (): Promise<ApiCategory[]> => {
  const categories = await getAllCategories();
  return categories.filter((category) => !category.isCommercial);
});

/**
 * Primary navigation: database categories, then the static links.
 *
 * If the API is unreachable, `getCategories()` returns an empty list and the nav
 * degrades to the static links rather than throwing. A missing section for one
 * render is recoverable; a 500 on every page is not.
 */
export async function getPrimaryNav(): Promise<{ href: string; label: string }[]> {
  const categories = await getNavCategories();
  return [
    ...categories.map((category) => ({ href: `/${category.slug}`, label: category.name })),
    ...STATIC_PRIMARY_NAV.map((item) => ({ href: item.href, label: item.label })),
  ];
}

/**
 * Look up one category by slug. Returns null when unknown, so callers decide whether
 * that means 404 (a category page) or an empty result set (a filtered list).
 */
export async function findCategory(slug: string): Promise<ApiCategory | null> {
  const categories = await getAllCategories();
  return categories.find((category) => category.slug === slug) ?? null;
}
