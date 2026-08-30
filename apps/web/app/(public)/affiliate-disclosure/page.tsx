import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Affiliate Disclosure",
  description: `How ${SITE.name} handles affiliate links, and the limits we place on them.`,
  alternates: { canonical: `${SITE.url}/affiliate-disclosure` },
};

export default function AffiliateDisclosurePage() {
  return (
    <PageShell
      title="Affiliate Disclosure"
      intro="Some of our commercial content contains affiliate links. Here is exactly what that means."
      updated="August 2026"
    >
      <p>
        If you buy something through a link in our product guides, The Drop may earn a
        commission from the retailer. It costs you nothing extra, and it does not change
        the price you pay.
      </p>

      <h2>Where affiliate links appear — and where they never do</h2>
      <p>
        Affiliate links appear only in clearly labeled commercial content: product guides,
        comparisons, roundups and deals, which live in our Picks section.
      </p>
      <p>
        They <strong>never</strong> appear in news, analysis, opinion or commentary. This
        is not a guideline we try to follow — our database physically cannot attach a
        commercial link to an article of those types.
      </p>

      <h2>What we do not claim</h2>
      <p>
        We do not test products. We will never tell you we did. Our guides are built from
        manufacturer specifications and retailer data, and we say so in the language we
        use: &ldquo;based on the available specifications,&rdquo; not &ldquo;we tried
        it.&rdquo;
      </p>
      <p>We also do not invent:</p>
      <ul>
        <li>prices, discounts or availability we cannot verify at the time of writing</li>
        <li>star ratings, review counts or customer quotes</li>
        <li>specifications the manufacturer has not published</li>
        <li>&ldquo;best&rdquo; rankings without stating the criteria we ranked on</li>
      </ul>
      <p>
        Where a price is shown, it was current when we fetched it and may have changed —
        which is why our buttons usually say &ldquo;check latest price&rdquo; rather than
        quoting a number at you.
      </p>

      <h2>Broken links</h2>
      <p>
        We check our outbound links on a schedule. When one breaks or expires, the button
        is hidden rather than left pointing somewhere useless.
      </p>

      <p>
        Questions about a specific recommendation? <Link href="/contact">Contact us.</Link>
      </p>
    </PageShell>
  );
}
