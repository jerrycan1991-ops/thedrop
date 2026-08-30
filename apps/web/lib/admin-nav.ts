/**
 * Admin information architecture.
 *
 * Every section from the specification is routed from day one. Sections whose feature
 * lands later are marked with the phase that fills them and render an honest
 * placeholder — a dead link is worse than a labeled one, and a nav that changes shape
 * every phase is disorienting.
 */
export interface AdminNavItem {
  href: string;
  label: string;
  /** Phase that implements it. Absent means it works now. */
  phase?: number;
}

export interface AdminNavGroup {
  label: string;
  items: AdminNavItem[];
}

export const ADMIN_NAV: AdminNavGroup[] = [
  {
    label: "Overview",
    items: [
      { href: "/admin", label: "Dashboard" },
      { href: "/admin/system-health", label: "System Health" },
      { href: "/admin/logs", label: "Logs" },
    ],
  },
  {
    label: "Newsroom",
    items: [
      { href: "/admin/incoming", label: "Incoming Stories", phase: 2 },
      { href: "/admin/viral-radar", label: "Viral Radar", phase: 3 },
      { href: "/admin/clusters", label: "Story Clusters", phase: 3 },
      { href: "/admin/drafts", label: "Drafts", phase: 4 },
      { href: "/admin/published", label: "Published" },
      { href: "/admin/rejected", label: "Rejected", phase: 4 },
      { href: "/admin/articles", label: "Articles" },
      { href: "/admin/corrections", label: "Corrections" },
    ],
  },
  {
    label: "Media",
    items: [
      { href: "/admin/media", label: "Media", phase: 6 },
      { href: "/admin/videos", label: "Videos", phase: 6 },
    ],
  },
  {
    label: "Affiliate",
    items: [
      { href: "/admin/affiliate/add", label: "Add Product", phase: 5 },
      { href: "/admin/affiliate/products", label: "Products", phase: 5 },
      { href: "/admin/affiliate/needs-metadata", label: "Needs Metadata", phase: 5 },
      { href: "/admin/affiliate/articles", label: "Generated Articles", phase: 5 },
      { href: "/admin/affiliate/links", label: "Affiliate Links", phase: 5 },
      { href: "/admin/affiliate/campaigns", label: "Campaigns", phase: 5 },
      { href: "/admin/affiliate/clicks", label: "Click Analytics", phase: 5 },
      { href: "/admin/affiliate/disclosures", label: "Disclosures", phase: 5 },
      { href: "/admin/affiliate/cta-templates", label: "CTA Templates", phase: 5 },
    ],
  },
  {
    label: "Sources",
    items: [
      { href: "/admin/sources", label: "Sources", phase: 2 },
      { href: "/admin/providers", label: "Providers", phase: 2 },
    ],
  },
  {
    label: "Business",
    items: [
      { href: "/admin/analytics", label: "Analytics", phase: 5 },
      { href: "/admin/revenue", label: "Revenue", phase: 5 },
      { href: "/admin/ai-costs", label: "AI Costs", phase: 4 },
      { href: "/admin/api-costs", label: "API Costs", phase: 4 },
    ],
  },
  {
    label: "Engineering",
    items: [
      { href: "/admin/prompts", label: "Prompt Versions", phase: 4 },
      { href: "/admin/experiments", label: "Experiments", phase: 8 },
      { href: "/admin/settings", label: "Settings" },
    ],
  },
];

/** Flat lookup used by the placeholder route to decide 404 vs "coming in phase N". */
export const ADMIN_ROUTES = new Map<string, AdminNavItem>(
  ADMIN_NAV.flatMap((group) => group.items.map((item) => [item.href, item])),
);
