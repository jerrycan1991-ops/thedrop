import Link from "next/link";

import { FOOTER_NAV, SITE } from "@thedrop/config";

import { DropLockup } from "@/components/brand/DropMark";
import { getNavCategories } from "@/lib/categories";

export async function SiteFooter() {
  const categories = await getNavCategories();

  return (
    <footer className="mt-20 border-t border-line bg-sunken">
      <div className="mx-auto max-w-[--page-width] px-4 py-14 sm:px-6">
        <div className="grid gap-10 md:grid-cols-[1.5fr_1fr_1fr]">
          <div>
            <DropLockup markSize={26} />
            <p className="dek mt-4 max-w-sm text-sm">{SITE.description}</p>
            <p className="meta mt-5">
              AI-assisted, human-governed.{" "}
              <Link href="/editorial-policy" className="underline underline-offset-2">
                How we work
              </Link>
            </p>
          </div>

          <nav aria-label="Sections">
            <h2 className="meta mb-3">Sections</h2>
            <ul className="space-y-2">
              {categories.map((category) => (
                <li key={category.slug}>
                  <Link
                    href={`/${category.slug}`}
                    className="text-sm text-muted transition-colors hover:text-fg"
                  >
                    {category.name}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>

          <nav aria-label="About this site">
            <h2 className="meta mb-3">The Drop</h2>
            <ul className="space-y-2">
              {FOOTER_NAV.map((item) => (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    className="text-sm text-muted transition-colors hover:text-fg"
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>

        <div className="mt-12 flex flex-col gap-3 border-t border-line pt-6 text-xs text-subtle sm:flex-row sm:items-center sm:justify-between">
          <p>
            © {new Date().getFullYear()} {SITE.name}. All rights reserved.
          </p>
          <p>
            Corrections and takedown requests:{" "}
            <Link href="/contact" className="underline underline-offset-2">
              contact us
            </Link>
          </p>
        </div>
      </div>
    </footer>
  );
}
