import Link from "next/link";

import { DropLockup } from "@/components/brand/DropMark";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { getPrimaryNav } from "@/lib/categories";

/**
 * Server component: navigation is built from the categories table, so adding a
 * section is a database row rather than a code change.
 */
export async function SiteHeader() {
  const primaryNav = await getPrimaryNav();

  return (
    <header className="sticky top-0 z-50 border-b border-line bg-bg/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-[--page-width] items-center gap-6 px-4 sm:px-6">
        <Link href="/" aria-label="The Drop home" className="shrink-0">
          <DropLockup markSize={30} />
        </Link>

        {/* Horizontal scroll rather than a wrap: the nav stays one line on narrow
            screens instead of pushing the header to two rows. */}
        <nav
          aria-label="Sections"
          className="scroll-x -mx-2 hidden flex-1 items-center gap-1 px-2 md:flex"
        >
          {primaryNav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm font-medium text-muted transition-colors hover:bg-surface-hover hover:text-fg"
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <Link
            href="/search"
            aria-label="Search"
            className="grid h-8 w-8 place-items-center rounded-full text-muted transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.6" />
              <path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </Link>
          <ThemeToggle />
        </div>
      </div>

      {/* Mobile section rail */}
      <nav
        aria-label="Sections"
        className="scroll-x flex gap-1 border-t border-line px-4 py-2 md:hidden"
      >
        {primaryNav.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="whitespace-nowrap rounded-full border border-line px-3 py-1 text-xs font-medium text-muted"
          >
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
