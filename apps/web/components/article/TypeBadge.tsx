import { ARTICLE_TYPES, COMMERCIAL_ARTICLE_TYPES } from "@thedrop/config";

import { cn } from "@/lib/utils";

/**
 * The article-type label.
 *
 * Always rendered, never optional. Distinguishing news from analysis from opinion is
 * an editorial obligation, so it lives in the template from day one rather than being
 * retrofitted once the first opinion piece ships.
 */
export function TypeBadge({ type, className }: { type: string; className?: string }) {
  const editorial = ARTICLE_TYPES[type as keyof typeof ARTICLE_TYPES];
  const commercial = COMMERCIAL_ARTICLE_TYPES[type as keyof typeof COMMERCIAL_ARTICLE_TYPES];

  if (!editorial && !commercial) return null;

  const label = editorial?.label ?? commercial?.label ?? type;
  const tone = editorial?.tone ?? "commercial";

  const toneClasses: Record<string, string> = {
    // Breaking is the only red in the system. Reserving it keeps it meaningful.
    breaking: "bg-breaking text-white",
    accent: "bg-accent-subtle text-accent-fg border border-accent/30",
    info: "bg-surface text-info border border-line",
    neutral: "bg-surface text-muted border border-line",
    commercial: "bg-surface text-warning border border-warning/30",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.08em]",
        toneClasses[tone] ?? toneClasses.neutral,
        className,
      )}
    >
      {label}
    </span>
  );
}
