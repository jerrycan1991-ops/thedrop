import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { CATEGORIES, SITE } from "@thedrop/config";

import { ArticleCard } from "@/components/article/ArticleCard";
import { getArticles, getCategories } from "@/lib/api-client";

export const revalidate = 120;

interface Props {
  params: Promise<{ category: string }>;
}

/**
 * Only known categories are pre-rendered. Unknown slugs fall through to a 404 rather
 * than generating a page — mass-generating empty category pages is exactly the
 * low-value indexing Google penalises.
 */
export async function generateStaticParams() {
  return CATEGORIES.map((category) => ({ category: category.slug }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { category: slug } = await params;
  const categories = await getCategories();
  const category = categories.find((c) => c.slug === slug);

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

  const categories = await getCategories();
  // Fall back to the static list so the page still renders if the API is down.
  const category =
    categories.find((c) => c.slug === slug) ??
    CATEGORIES.find((c) => c.slug === slug);

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
        {"description" in category && category.description && (
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
