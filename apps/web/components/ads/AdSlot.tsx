import type { AdPlacement } from "@thedrop/config";

interface AdSlotProps {
  placement: AdPlacement;
  category?: string;
  riskTier?: string;
  className?: string;
}

/**
 * Generic ad slot. Business logic never imports an ad network (MONETIZATION.md §1).
 *
 * Two rules are enforced here rather than in configuration, because they are the two
 * that protect the site:
 *
 *  1. **High-risk stories get no ads.** Deaths, crime, war, tragedy. This protects
 *     readers, and it protects the AdSense account — policy strikes on sensitive-content
 *     placement are the fastest way to lose it.
 *  2. **An ineligible slot renders nothing at all**, not an empty container. A reserved
 *     box for an ad that will never load is pure layout shift.
 *
 * When a provider IS active, the wrapper reserves its height before load so ads cannot
 * damage CLS — a Core Web Vitals regression costs more traffic than the ad earns.
 */
export function AdSlot({ placement, riskTier, className }: AdSlotProps) {
  const adsEnabled = process.env.NEXT_PUBLIC_ADS_ENABLED === "true";

  if (!adsEnabled) return null;
  if (riskTier === "high") return null;

  return (
    <div
      className={className}
      data-ad-placement={placement}
      // Phase 5 swaps this for the resolved provider component. The reserved height
      // is deliberately in place from the start so the layout is already correct.
      style={{ minHeight: 250 }}
      aria-hidden="true"
    />
  );
}
