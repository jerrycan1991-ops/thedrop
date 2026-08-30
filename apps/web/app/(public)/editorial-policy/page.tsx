import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Editorial Policy",
  description: `The standards ${SITE.name} holds itself to, and how they are enforced.`,
  alternates: { canonical: `${SITE.url}/editorial-policy` },
};

export default function EditorialPolicyPage() {
  return (
    <PageShell
      title="Editorial Policy"
      intro="Our standards, and the mechanisms that enforce them."
      updated="August 2026"
    >
      <h2>Verification</h2>
      <p>
        Before publication, every factual claim in an article is extracted, typed and
        checked. A claim reaches print as a statement of fact only when it is corroborated
        by two independent credible sources, or supported by a directly relevant
        authoritative primary source — a court filing, a regulator, an official statement.
      </p>
      <p>
        Anything less keeps its attribution. A single-sourced report is described as a
        report. An accusation is described as an accusation. We never collapse
        &ldquo;someone said X&rdquo; into &ldquo;X.&rdquo;
      </p>

      <h2>High-risk stories</h2>
      <p>
        Elections, crime, deaths, legal accusations, health claims, market-moving
        financial claims, war, public safety, and reports about named individuals are held
        to a stricter standard. If a load-bearing claim in one of these stories is not
        corroborated or authoritative, the story is deferred — not published with a hedge.
      </p>

      <h2>Labels</h2>
      <p>
        <strong>News</strong> is evidence-based and contains no opinion.{" "}
        <strong>Analysis</strong> interprets evidence and says so. <strong>Opinion</strong>{" "}
        and <strong>Commentary</strong> argue a position; their factual premises are still
        sourced. Labels are shown on every article, in listings and on the article page.
      </p>

      <h2>Sources and originality</h2>
      <p>
        We link to and briefly quote the reporting we rely on, with attribution. We do not
        republish other outlets&rsquo; articles, and we do not rewrite them: our articles
        are written from a structured evidence packet, not from another publisher&rsquo;s
        prose. Source links appear at the foot of every article.
      </p>

      <h2>Imagery</h2>
      <p>
        Our illustrations are generated and original. They are labeled as AI-generated
        wherever they appear. We do not generate photorealistic depictions of real people,
        and we never present a generated image as documentary photography of a real event.
        We do not copy or recreate another publisher&rsquo;s photography.
      </p>

      <h2>Automation and human accountability</h2>
      <p>
        Discovery, verification, writing and publishing are automated. Publication is gated
        by confidence thresholds that software enforces — an article that fails
        verification cannot be published by any part of the system, including the part that
        wrote it. Humans set the standards, review the failures, and answer for the output.
      </p>

      <h2>Independence</h2>
      <p>
        Commercial considerations do not determine what we cover or how we cover it.
        Advertising is not shown on stories about death, crime, tragedy or conflict.
        Affiliate links never appear in news, analysis, opinion or commentary — they are
        confined to clearly labeled commercial content. See our{" "}
        <Link href="/affiliate-disclosure">affiliate disclosure</Link>.
      </p>

      <h2>Corrections</h2>
      <p>
        Errors are corrected publicly and permanently on the article and on our{" "}
        <Link href="/corrections">corrections page</Link>. Substantive corrections are
        described, not quietly edited away. Retractions are marked as such.{" "}
        <Link href="/contact">Report an error.</Link>
      </p>
    </PageShell>
  );
}
