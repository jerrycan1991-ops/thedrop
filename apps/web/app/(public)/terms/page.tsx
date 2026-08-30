import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Terms of Use",
  description: `Terms governing use of ${SITE.name}.`,
  alternates: { canonical: `${SITE.url}/terms` },
};

export default function TermsPage() {
  return (
    <PageShell title="Terms of Use" updated="August 2026">
      <p className="text-sm text-subtle">
        This is a plain-language summary written for clarity, not a substitute for legal
        review. Have counsel review it before launch.
      </p>

      <h2>Using this site</h2>
      <p>
        You may read, link to and share our articles. You may quote short extracts with
        attribution and a link. You may not republish articles in full, scrape the site at
        scale, or use our content to train a model without written permission.
      </p>

      <h2>Our content</h2>
      <p>
        Articles and original imagery on this site are ours. Third-party material we
        reference remains the property of its owner and is used with attribution and a
        link, under fair use. If you believe we have used your material improperly,{" "}
        <Link href="/contact">tell us</Link> and we will review it promptly.
      </p>

      <h2>Accuracy</h2>
      <p>
        We verify before we publish and correct publicly when we are wrong. News is
        provisional by nature: reporting evolves and articles may be updated. Nothing here
        is legal, financial, medical or professional advice, and you should not act on it
        as if it were.
      </p>

      <h2>Commercial content</h2>
      <p>
        Some content contains affiliate links, disclosed on the page and explained in our{" "}
        <Link href="/affiliate-disclosure">affiliate disclosure</Link>. We are not the
        seller; purchases are governed by the retailer&rsquo;s own terms, and we are not
        responsible for their products, prices, delivery or service.
      </p>

      <h2>Availability</h2>
      <p>
        The site is provided as-is. We do not guarantee uninterrupted availability, and we
        may change or remove content at any time.
      </p>

      <h2>Changes</h2>
      <p>
        We will update these terms as the site develops, and the date above will change
        when we do.
      </p>
    </PageShell>
  );
}
