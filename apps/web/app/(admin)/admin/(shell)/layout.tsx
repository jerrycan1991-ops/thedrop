import Link from "next/link";

import { DropMark } from "@/components/brand/DropMark";
import { ADMIN_NAV } from "@/lib/admin-nav";

// The admin must never be served from cache, and never indexed.
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Admin",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh bg-sunken">
      <aside className="hidden w-60 shrink-0 border-r border-line bg-bg lg:block">
        <div className="sticky top-0 flex h-full max-h-dvh flex-col">
          <Link href="/admin" className="flex items-center gap-2 border-b border-line px-4 py-4">
            <DropMark size={26} />
            <span className="display text-sm tracking-tight">ADMIN</span>
          </Link>

          <nav aria-label="Admin sections" className="flex-1 overflow-y-auto p-2">
            {ADMIN_NAV.map((group) => (
              <div key={group.label} className="mb-4">
                <p className="meta px-2 pb-1.5">{group.label}</p>
                <ul>
                  {group.items.map((item) => (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm text-muted transition-colors hover:bg-surface-hover hover:text-fg"
                      >
                        <span>{item.label}</span>
                        {item.phase && (
                          <span className="rounded-sm bg-surface px-1 text-[9px] font-semibold text-subtle">
                            P{item.phase}
                          </span>
                        )}
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>

          <div className="border-t border-line p-3">
            <Link href="/" className="text-xs text-subtle hover:text-fg">
              ← View site
            </Link>
          </div>
        </div>
      </aside>

      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
