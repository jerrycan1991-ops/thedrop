import type { Metadata } from "next";

import { ArticleCard } from "@/components/article/ArticleCard";
import { getLatest } from "@/lib/api-client";

export const metadata: Metadata = {
  title: "Search",
  description: "Search The Drop.",
  // Search result pages are thin and near-infinite; indexing them is exactly the
  // low-value mass-generated content Google penalises.
  robots: { index: false, follow: true },
};

interface Props {
  searchParams: Promise<{ q?: string }>;
}

export default async function SearchPage({ searchParams }: Props) {
  const { q } = await searchParams;
  const query = (q ?? "").trim();

  // Phase 1 renders the interface and recent stories. Full-text search over the
  // Postgres tsvector index lands with real content in Phase 2 (docs/TASKS.md).
  const { items } = await getLatest(12);

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
      <h1 className="display text-4xl">Search</h1>

      <form action="/search" method="get" role="search" className="mt-6 flex gap-2">
        <input
          type="search"
          name="q"
          defaultValue={query}
          placeholder="Search stories…"
          aria-label="Search stories"
          className="h-11 flex-1 rounded-md border border-line bg-surface px-4 text-base text-fg placeholder:text-subtle"
        />
        <button
          type="submit"
          className="h-11 rounded-md bg-accent px-5 text-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover"
        >
          Search
        </button>
      </form>

      {query && (
        <p className="dek mt-6 text-sm">
          Full-text search arrives with the first published stories. In the meantime,
          here is the most recent coverage.
        </p>
      )}

      <section className="mt-8 border-t border-line pt-6">
        <h2 className="meta mb-4">Recent</h2>
        {items.length === 0 ? (
          <p className="dek text-sm">Nothing published yet.</p>
        ) : (
          <div className="grid gap-x-10 sm:grid-cols-2">
            {items.map((article) => (
              <ArticleCard key={article.id} article={article} variant="list" />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
