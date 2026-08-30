import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Corrections",
  description: `Every correction, clarification and retraction issued by ${SITE.name}.`,
  alternates: { canonical: `${SITE.url}/corrections` },
};

export const revalidate = 300;

export default function CorrectionsPage() {
  // Populated from the corrections table once articles are live (Phase 4). The page
  // exists from day one because a corrections policy without a corrections page is
  // not a policy.
  const corrections: { headline: string; path: string; type: string; detail: string; issuedAt: string }[] = [];

  return (
    <PageShell
      title="Corrections"
      intro="When we get something wrong, we say so here — permanently."
    >
      <p>
        Corrections also appear on the article they relate to. Retractions are marked and
        removed from search indexing. If you have found an error,{" "}
        <Link href="/contact">tell us</Link> and we will look at it.
      </p>

      {corrections.length === 0 ? (
        <p className="text-muted">No corrections have been issued.</p>
      ) : (
        <ul>
          {corrections.map((correction, index) => (
            <li key={index}>
              <Link href={correction.path}>{correction.headline}</Link>
              <p>{correction.detail}</p>
            </li>
          ))}
        </ul>
      )}
    </PageShell>
  );
}
