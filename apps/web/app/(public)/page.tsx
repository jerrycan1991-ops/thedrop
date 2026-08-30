import Link from "next/link";

import { CATEGORIES } from "@thedrop/config";

import { AdSlot } from "@/components/ads/AdSlot";
import { ArticleCard } from "@/components/article/ArticleCard";
import { getArticles, getLatest, type ApiArticleSummary } from "@/lib/api-client";

// The homepage is the most-hit path and changes constantly. A 60s ISR window means
// almost every visitor is served static HTML with no Python and no database involved.
export const revalidate = 60;

function SectionHeading({ title, href }: { title: string; href?: string }) {
  return (
    <div className="mb-5 flex items-baseline justify-between gap-4 border-b border-line pb-2">
      <h2 className="display text-xl">{title}</h2>
      {href && (
        <Link href={href} className="meta transition-colors hover:text-accent-fg">
          View all →
        </Link>
      )}
    </div>
  );
}

/** Shown before the first articles exist, and if the API is unreachable. */
function EmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-line-strong bg-surface px-6 py-16 text-center">
      <p className="headline text-lg">Nothing published yet</p>
      <p className="dek mx-auto mt-2 max-w-md text-sm">
        The newsroom is running. Stories appear here once they clear verification — we
        publish when the evidence is there, not when a schedule says so.
      </p>
    </div>
  );
}

export default async function HomePage() {
  const [latest, ...categoryFeeds] = await Promise.all([
    getLatest(24),
    ...CATEGORIES.slice(0, 4).map((category) =>
      getArticles({ category: category.slug, pageSize: 4 }),
    ),
  ]);

  const articles: ApiArticleSummary[] = latest.items;
  const [lead, ...rest] = articles;
  const secondary = rest.slice(0, 4);
  const trending = rest.slice(4, 10);
  const more = rest.slice(10);

  return (
    <div className="mx-auto max-w-[--page-width] px-4 py-8 sm:px-6">
      {!lead ? (
        <EmptyState />
      ) : (
        <>
          {/* Lead + trending rail */}
          <section className="grid gap-10 lg:grid-cols-[2fr_1fr]">
            <div>
              <ArticleCard article={lead} variant="hero" priority />

              {secondary.length > 0 && (
                <div className="mt-10 grid gap-8 sm:grid-cols-2">
                  {secondary.map((article) => (
                    <ArticleCard key={article.id} article={article} />
                  ))}
                </div>
              )}
            </div>

            <aside>
              <SectionHeading title="Trending" href="/trending" />
              {trending.length > 0 ? (
                <ol className="divide-y divide-[--border]">
                  {trending.map((article, index) => (
                    <li key={article.id} className="flex gap-3">
                      <span
                        className="display mt-3 w-6 shrink-0 text-lg text-subtle"
                        aria-hidden="true"
                      >
                        {index + 1}
                      </span>
                      <ArticleCard article={article} variant="compact" className="flex-1" />
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="dek text-sm">No trending stories yet.</p>
              )}

              <AdSlot placement="sidebar" className="mt-8" />

              <div className="mt-8 rounded-lg border border-line bg-surface p-5">
                <h2 className="headline text-base">The Drop, daily</h2>
                <p className="dek mt-1.5 text-sm">
                  One email each morning. What happened, what it means, nothing padded.
                </p>
                <Link
                  href="/newsletter"
                  className="mt-4 inline-flex h-9 items-center rounded-md bg-accent px-4 text-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover"
                >
                  Subscribe
                </Link>
              </div>
            </aside>
          </section>

          <AdSlot placement="home_module" className="my-12" />

          {/* Category modules */}
          {CATEGORIES.slice(0, 4).map((category, index) => {
            const feed = categoryFeeds[index];
            if (!feed || feed.items.length === 0) return null;
            return (
              <section key={category.slug} className="mt-14">
                <SectionHeading title={category.name} href={`/${category.slug}`} />
                <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
                  {feed.items.map((article) => (
                    <ArticleCard key={article.id} article={article} />
                  ))}
                </div>
              </section>
            );
          })}

          {more.length > 0 && (
            <section className="mt-14">
              <SectionHeading title="Latest" href="/latest" />
              <div className="grid gap-x-10 sm:grid-cols-2">
                {more.map((article) => (
                  <ArticleCard key={article.id} article={article} variant="list" />
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
