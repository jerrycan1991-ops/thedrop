import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { notFound } from "next/navigation";

import { SITE } from "@thedrop/config";

import { AdSlot } from "@/components/ads/AdSlot";
import { ArticleBody } from "@/components/article/ArticleBody";
import { TypeBadge } from "@/components/article/TypeBadge";
import { ApiError, getArticle } from "@/lib/api-client";
import { formatDate, formatReadingTime, formatTime } from "@/lib/utils";

export const revalidate = 120;

interface Props {
  params: Promise<{
    category: string;
    year: string;
    month: string;
    day: string;
    slug: string;
  }>;
}

async function load(props: Props) {
  const { category, year, month, day, slug } = await props.params;
  try {
    return await getArticle(category, year, month, day, slug);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }
}

export async function generateMetadata(props: Props): Promise<Metadata> {
  const article = await load(props);

  return {
    title: article.seo.title,
    description: article.seo.metaDescription,
    alternates: { canonical: `${SITE.url}${article.seo.canonicalUrl}` },
    robots: article.seo.noindex ? { index: false, follow: true } : undefined,
    openGraph: {
      type: "article",
      title: article.seo.ogTitle,
      description: article.seo.ogDescription,
      url: `${SITE.url}${article.path}`,
      publishedTime: article.publishedAt ?? undefined,
      modifiedTime: article.updatedAt ?? undefined,
      section: article.category.name,
      images: article.heroImage
        ? [{ url: article.heroImage.url, width: article.heroImage.width, height: article.heroImage.height, alt: article.heroImage.altText }]
        : undefined,
    },
    twitter: {
      card: "summary_large_image",
      title: article.seo.ogTitle,
      description: article.seo.ogDescription,
    },
  };
}

export default async function ArticlePage(props: Props) {
  const article = await load(props);
  const hero = article.heroImage;

  return (
    <article className="mx-auto max-w-[--page-width] px-4 py-10 sm:px-6">
      {/* Generated server-side from stored fields, never hand-authored and never
          model-authored (docs/PIPELINE.md). */}
      {Object.keys(article.structuredData).length > 0 && (
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(article.structuredData) }}
        />
      )}

      <div className="mx-auto max-w-[--content-width]">
        <nav aria-label="Breadcrumb" className="meta">
          <Link href="/" className="hover:text-fg">
            Home
          </Link>
          <span aria-hidden="true"> / </span>
          <Link
            href={`/${article.category.slug}`}
            className="hover:text-fg"
            style={{ color: `var(${article.category.accentToken})` }}
          >
            {article.category.name}
          </Link>
        </nav>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <TypeBadge type={article.articleType} />
          {article.isSponsored && <span className="meta text-warning">Sponsored</span>}
        </div>

        <h1 className="display mt-3 text-4xl sm:text-5xl">{article.headline}</h1>
        {article.dek && <p className="dek mt-4 text-lg">{article.dek}</p>}

        <div className="mt-6 flex flex-wrap items-center gap-x-3 gap-y-1 border-y border-line py-3">
          <span className="text-sm font-medium">{article.byline}</span>
          <span className="meta">
            {formatDate(article.publishedAt)} · {formatTime(article.publishedAt)}
          </span>
          <span className="meta">{formatReadingTime(article.readingTimeSeconds)}</span>
          {article.updatedAt && (
            <span className="meta text-accent-fg">Updated {formatDate(article.updatedAt)}</span>
          )}
        </div>
      </div>

      {hero && (
        <figure className="mx-auto mt-8 max-w-4xl">
          <div className="relative aspect-[16/9] overflow-hidden rounded-lg bg-surface">
            <Image
              src={hero.url}
              alt={hero.altText}
              fill
              priority
              sizes="(max-width: 1024px) 100vw, 900px"
              className="object-cover"
            />
          </div>
          {(hero.caption || hero.credit || hero.isAiGenerated) && (
            <figcaption className="mt-2 flex flex-wrap gap-x-2 text-xs text-subtle">
              {hero.caption && <span>{hero.caption}</span>}
              {hero.credit && <span>({hero.credit})</span>}
              {/* Required, not optional: an AI illustration must never be mistaken for
                  a photograph of a real event. */}
              {hero.isAiGenerated && (
                <span className="font-semibold uppercase tracking-wide">
                  {hero.aiDisclosure ?? "AI-generated illustration"}
                </span>
              )}
            </figcaption>
          )}
        </figure>
      )}

      <div className="mx-auto mt-10 max-w-[--content-width]">
        {article.disclosure && (
          <aside className="mb-8 rounded-md border border-warning/30 bg-warning-subtle px-4 py-3 text-sm">
            {article.disclosure}
          </aside>
        )}

        {article.keyFacts.length > 0 && (
          <aside className="mb-8 rounded-lg border border-line bg-surface p-5">
            <h2 className="meta mb-3">What to know</h2>
            <ul className="space-y-2 text-[0.95rem]">
              {article.keyFacts.map((fact, index) => (
                <li key={index} className="flex gap-2.5">
                  <span aria-hidden="true" className="mt-2 h-1 w-3 shrink-0 bg-accent" />
                  <span>{fact}</span>
                </li>
              ))}
            </ul>
          </aside>
        )}

        <ArticleBody blocks={article.body} riskTier="standard" />

        <AdSlot placement="article_end" className="my-10" />

        {article.corrections.length > 0 && (
          <section className="mt-10 rounded-lg border border-danger/30 bg-danger-subtle p-5">
            <h2 className="headline text-base">Corrections</h2>
            <ul className="mt-3 space-y-3 text-sm">
              {article.corrections.map((correction, index) => (
                <li key={index}>
                  <span className="meta">
                    {correction.type} · {formatDate(correction.issuedAt)}
                  </span>
                  <p className="mt-1">{correction.detail}</p>
                </li>
              ))}
            </ul>
          </section>
        )}

        {article.sources.length > 0 && (
          <section className="mt-10 border-t border-line pt-6">
            <h2 className="meta mb-3">Sources</h2>
            <ol className="space-y-2 text-sm">
              {article.sources.map((source, index) => (
                <li key={index} className="flex gap-2">
                  <span className="text-subtle">{index + 1}.</span>
                  <a
                    href={source.url}
                    rel="noopener noreferrer nofollow"
                    target="_blank"
                    className="text-muted underline underline-offset-2 hover:text-fg"
                  >
                    <span className="font-medium">{source.publisher}</span> — {source.title}
                  </a>
                </li>
              ))}
            </ol>
          </section>
        )}

        {article.tags.length > 0 && (
          <div className="mt-8 flex flex-wrap gap-2">
            {article.tags.map((tag) => (
              <span
                key={tag.slug}
                className="rounded-full border border-line px-3 py-1 text-xs text-muted"
              >
                {tag.name}
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}
