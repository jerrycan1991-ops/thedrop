/**
 * THE DROP identity — PLACEHOLDER.
 *
 * A geometric "D" built from a rounded-square counter and a solid stem, with the
 * counter cut as a falling drop. It is deliberately constructed from primitives
 * rather than drawn: it stays legible at 16px (favicon), reads as a single silhouette
 * in a 24px avatar crop, and survives being stamped on a video frame as a watermark.
 *
 * NO TRADEMARK CLAIM IS MADE. This is a working placeholder to be replaced by a final
 * mark from a designer, with clearance, before launch. See docs/BRAND.md.
 */

interface DropMarkProps {
  size?: number;
  className?: string;
  /** Solid ground behind the mark. Off for inline use on a coloured surface. */
  withPlate?: boolean;
  title?: string;
}

export function DropMark({
  size = 32,
  className,
  withPlate = true,
  title = "The Drop",
}: DropMarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role="img"
      aria-label={title}
    >
      {withPlate && <rect width="48" height="48" rx="11" fill="var(--accent)" />}
      {/* The D: a stem plus a bowl, with the counter shaped as a falling drop.
          Even-odd fill keeps the counter transparent at every size. */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M13 11h11.2c8.7 0 14.3 5.1 14.3 13s-5.6 13-14.3 13H13V11zm7.8 6.6v12.8h3.2c4.2 0 6.7-2.4 6.7-6.4s-2.5-6.4-6.7-6.4h-3.2z"
        fill={withPlate ? "var(--fg-on-accent)" : "currentColor"}
      />
    </svg>
  );
}

/** Full lockup: mark + wordmark. Used in the header and the footer. */
export function DropLockup({
  className,
  markSize = 28,
}: {
  className?: string;
  markSize?: number;
}) {
  return (
    <span className={className} style={{ display: "inline-flex", alignItems: "center", gap: "0.55rem" }}>
      <DropMark size={markSize} />
      <span
        className="display"
        style={{ fontSize: markSize * 0.62, letterSpacing: "-0.045em", lineHeight: 1 }}
      >
        THE DROP
      </span>
    </span>
  );
}
