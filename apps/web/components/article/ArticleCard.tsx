import Image from "next/image";
import Link from "next/link";

import type { ApiArticleSummary } from "@/lib/api-client";
import { TypeBadge } from "@/components/article/TypeBadge";
import { cn, formatReadingTime, relativeTime } from "@/lib/utils";

interface ArticleCardProps {
  article: ApiArticleSummary;
  variant?: "hero" | "standard" | "compact" | "list";
  priority?: boolean;
  className?: string;
}

export function ArticleCard({
  article,
  variant = "standard",
  priority = false,
  className,
}: ArticleCardProps) {
  const hero = article.heroImage;
  const accent = article.category.accentToken;

  if (variant === "compact" || variant === "list") {
    return (
      <article className={cn("group flex gap-3 py-3", className)}>
        {variant === "list" && (
          <span
            aria-hidden="true"
            className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
            style={{ backgroundColor: `var(${accent})` }}
          />
        )}
        <div className="min-w-0">
          <Link href={article.path} className="block">
            <h3 className="headline text-[0.95rem] transition-colors group-hover:text-accent-fg">
              {article.headline}
            </h3>
          </Link>
          <p className="meta mt-1.5">
            {article.category.name} · {relativeTime(article.publishedAt)}
          </p>
        </div>
      </article>
    );
  }

  const isHero = variant === "hero";

  return (
    <article className={cn("group", className)}>
      <Link href={article.path} className="block">
        {hero ? (
          <div
            className={cn(
              "relative overflow-hidden rounded-lg bg-surface",
              isHero ? "aspect-[16/9]" : "aspect-[3/2]",
            )}
          >
            <Image
              src={hero.url}
              alt={hero.altText}
              fill
              priority={priority}
              sizes={isHero ? "(max-width: 768px) 100vw, 66vw" : "(max-width: 768px) 100vw, 33vw"}
              className="object-cover transition-transform duration-500 group-hover:scale-[1.03]"
            />
            {/* Generated imagery is always labeled. An AI illustration must never be
                mistaken for documentary photography of a real event. */}
            {hero.isAiGenerated && (
              <span className="absolute bottom-2 right-2 rounded-sm bg-black/70 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-white/90">
                AI illustration
              </span>
            )}
          </div>
        ) : (
          <div
            className={cn(
              "rounded-lg border border-line bg-surface",
              isHero ? "aspect-[16/9]" : "aspect-[3/2]",
            )}
            style={{ boxShadow: "var(--highlight-top)" }}
            aria-hidden="true"
          />
        )}
      </Link>

      <div className={cn("mt-3", isHero && "mt-4")}>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href={`/${article.category.slug}`}
            className="text-[11px] font-bold uppercase tracking-[0.08em] transition-opacity hover:opacity-80"
            style={{ color: `var(${accent})` }}
          >
            {article.category.name}
          </Link>
          <TypeBadge type={article.articleType} />
          {article.isSponsored && (
            <span className="meta text-warning">Sponsored</span>
          )}
        </div>

        <Link href={article.path} className="block">
          <h2
            className={cn(
              "headline mt-2 transition-colors group-hover:text-accent-fg",
              isHero ? "text-3xl sm:text-4xl" : "text-lg",
            )}
          >
            {article.headline}
          </h2>
        </Link>

        {(isHero || variant === "standard") && article.dek && (
          <p className={cn("dek mt-2", !isHero && "text-sm")}>{article.dek}</p>
        )}

        <p className="meta mt-3">
          {relativeTime(article.publishedAt)} · {formatReadingTime(article.readingTimeSeconds)}
        </p>
      </div>
    </article>
  );
}
