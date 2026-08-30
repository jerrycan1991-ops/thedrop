import type { MetadataRoute } from "next";

import { SITE } from "@thedrop/config";

import { getLatest } from "@/lib/api-client";
import { getAllCategories } from "@/lib/categories";

export const revalidate = 600;

/**
 * Standard sitemap: static pages, sections, and published articles.
 *
 * The Google News sitemap is a separate route (Phase 5) with different rules — it
 * carries only articles from the last 48 hours, and excludes the commercial /picks
 * section entirely, so scaled affiliate output cannot affect news standing.
 */
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const staticPages = [
    { path: "", priority: 1.0, changeFrequency: "hourly" as const },
    { path: "/latest", priority: 0.9, changeFrequency: "hourly" as const },
    { path: "/about", priority: 0.5, changeFrequency: "monthly" as const },
    { path: "/editorial-policy", priority: 0.5, changeFrequency: "monthly" as const },
    { path: "/corrections", priority: 0.4, changeFrequency: "weekly" as const },
    { path: "/contact", priority: 0.3, changeFrequency: "yearly" as const },
    { path: "/affiliate-disclosure", priority: 0.3, changeFrequency: "yearly" as const },
    { path: "/privacy", priority: 0.2, changeFrequency: "yearly" as const },
    { path: "/terms", priority: 0.2, changeFrequency: "yearly" as const },
    { path: "/newsletter", priority: 0.4, changeFrequency: "monthly" as const },
  ];

  const entries: MetadataRoute.Sitemap = staticPages.map((page) => ({
    url: `${SITE.url}${page.path}`,
    lastModified: new Date(),
    changeFrequency: page.changeFrequency,
    priority: page.priority,
  }));

  // Every active category, including commercial sections — they belong in the
  // standard sitemap even though they are excluded from the Google News one.
  for (const category of await getAllCategories()) {
    entries.push({
      url: `${SITE.url}/${category.slug}`,
      lastModified: new Date(),
      changeFrequency: "hourly",
      priority: 0.8,
    });
  }

  // safeFetch returns an empty list if the API is down, so a backend blip produces a
  // smaller sitemap rather than a build failure.
  const { items } = await getLatest(500);
  for (const article of items) {
    entries.push({
      url: `${SITE.url}${article.path}`,
      lastModified: article.updatedAt ? new Date(article.updatedAt) : new Date(article.publishedAt ?? Date.now()),
      changeFrequency: "weekly",
      priority: 0.7,
    });
  }

  return entries;
}
