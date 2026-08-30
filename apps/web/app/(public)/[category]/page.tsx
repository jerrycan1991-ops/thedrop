import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { SITE } from "@thedrop/config";

import { ArticleCard } from "@/components/article/ArticleCard";
import { getArticles } from "@/lib/api-client";
import { findCategory, getAllCategories } from "@/lib/categories";

export const revalidate = 120;

interface Props {
  params: Promise<{ category: string }>;
}

/**
 * Pre-renders the categories that exist in the database at build time.
 *
 * `dynamicParams` stays at its default of true, so a category added after a build
 * still renders on demand — the point of making the table authoritative is that
 * adding one needs no deploy. Unknown slugs still 404 via `notFound()` below, so this
 * does not mass-generate empty pages.
 *
 * If the API is unreachable during a build this returns an empty list: nothing is
 * pre-rendered, every category page renders on demand, and the build still succeeds.
 */
export async function generateStaticParams() {
  const categories = await getAllCategories();
  return categories.map((category) => ({ category: category.slug }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { category: slug } = await params;
  const category = await findCategory(slug);

  if (!category) return {};

  const title = `${category.name} News`;
  const description =
    category.description ?? `The latest ${category.name.toLowerCase()} coverage from ${SITE.name}.`;

  return {
    title,
    description,
    alternates: { canonical: `${SITE.url}/${category.slug}` },
    openGraph: { title, description, url: `${SITE.url}/${category.slug}`, type: "website" },
  };
}

export default async function CategoryPage({ params }: Props) {
  const { category: slug } = await params;

  const category = await findCategory(slug);

  // A slug that is not a category is a 404 page — distinct from the API, where an
  // unknown `?category=` filter returns 200 with an empty list (pinned by the Phase 0
  // baseline). A page is a lookup; a query parameter is a filter.
  if (!category) notFound();

  const feed = await getArticles({ category: slug, pageSize: 24 });
  const [lead, ...rest] = feed.items;

  return (
    <div className="mx-auto max-w-[--page-width] px-4 py-10 sm:px-6">
      <header className="border-b border-line pb-6">
        <h1
          className="display text-4xl sm:text-5xl"
          style={{ color: `var(${category.accentToken})` }}
        >
          {category.name}
        </h1>
        {category.description && (
          <p className="dek mt-3 max-w-2xl">{category.description}</p>
        )}
      </header>

      {!lead ? (
        <p className="dek py-16 text-center">No published stories in this section yet.</p>
      ) : (
        <>
          <div className="mt-10">
            <ArticleCard article={lead} variant="hero" priority />
          </div>
          <div className="mt-12 grid gap-10 sm:grid-cols-2 lg:grid-cols-3">
            {rest.map((article) => (
              <ArticleCard key={article.id} article={article} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
