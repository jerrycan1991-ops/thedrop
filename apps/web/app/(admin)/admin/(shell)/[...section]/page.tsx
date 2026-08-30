import { notFound } from "next/navigation";

import { ADMIN_ROUTES } from "@/lib/admin-nav";

/**
 * Routed placeholder for admin sections whose feature lands in a later phase.
 *
 * Only paths that exist in the nav resolve; anything else is a genuine 404. That keeps
 * the navigation honest — every link works, and each one states plainly what it is
 * waiting on rather than pretending to be an empty implemented screen.
 */
export default async function AdminSectionPlaceholder({
  params,
}: {
  params: Promise<{ section: string[] }>;
}) {
  const { section } = await params;
  const href = `/admin/${section.join("/")}`;
  const item = ADMIN_ROUTES.get(href);

  if (!item) notFound();

  return (
    <div className="p-6 lg:p-8">
      <header className="mb-8">
        <h1 className="display text-2xl">{item.label}</h1>
        <p className="meta mt-1">{href}</p>
      </header>

      <div className="max-w-lg rounded-lg border border-dashed border-line-strong bg-surface p-6">
        <p className="headline text-base">
          {item.phase ? `Arrives in Phase ${item.phase}` : "Not yet implemented"}
        </p>
        <p className="dek mt-2 text-sm">
          The route, navigation and permissions for this section are in place. The feature
          behind it is scheduled — see <code>docs/ROADMAP.md</code>.
        </p>
      </div>
    </div>
  );
}
