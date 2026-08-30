import type { Metadata } from "next";

import { SITE } from "@thedrop/config";

import { ArticleCard } from "@/components/article/ArticleCard";
import { getLatest } from "@/lib/api-client";

export const revalidate = 30;

export const metadata: Metadata = {
  title: "Latest",
  description: `Everything ${SITE.name} has published, newest first.`,
  alternates: { canonical: `${SITE.url}/latest` },
};

export default async function LatestPage() {
  const { items } = await getLatest(40);

  return (
    <div className="mx-auto max-w-[--page-width] px-4 py-10 sm:px-6">
      <header className="border-b border-line pb-6">
        <h1 className="display text-4xl">Latest</h1>
        <p className="dek mt-2">Newest first. Updated continuously.</p>
      </header>

      {items.length === 0 ? (
        <p className="dek py-16 text-center">Nothing published yet.</p>
      ) : (
        <div className="mt-8 grid gap-x-12 sm:grid-cols-2">
          {items.map((article) => (
            <ArticleCard key={article.id} article={article} variant="list" />
          ))}
        </div>
      )}
    </div>
  );
}
