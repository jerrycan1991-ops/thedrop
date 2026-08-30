import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "About",
  description: `What ${SITE.name} is, how it works, and who is accountable for it.`,
  alternates: { canonical: `${SITE.url}/about` },
};

export default function AboutPage() {
  return (
    <PageShell
      title="About The Drop"
      intro="Fast, verified, US-first news — built by an automated newsroom with human accountability."
    >
      <h2>What we do</h2>
      <p>
        The Drop covers US politics, business, technology, sports, entertainment and the
        world stories that matter to an American audience. We publish roughly 20 to 30
        pieces a day, and only when the evidence supports them.
      </p>

      <h2>How this is made</h2>
      <p>
        Our reporting pipeline is automated and AI-assisted. Software discovers stories
        across many sources, groups the coverage of a single event, extracts the specific
        claims being made, and checks those claims against other independent reporting and
        primary documents. An AI system then writes an original article from that verified
        evidence.
      </p>
      <p>
        We are explicit about this because you deserve to know. What we do not do is
        rewrite someone else&rsquo;s article. Every piece is generated from a structured
        evidence packet — claims, sources, documents, timelines, and the things that are
        still unknown — rather than from another publisher&rsquo;s prose.
      </p>

      <h2>What we will not do</h2>
      <ul>
        <li>Publish a fact we cannot trace to a source.</li>
        <li>Turn &ldquo;someone claims X&rdquo; into &ldquo;X happened.&rdquo;</li>
        <li>Report a death, arrest or criminal charge without authoritative confirmation.</li>
        <li>Run a headline that the article does not support, however well it would perform.</li>
        <li>Publish to hit a daily number. If fewer stories clear verification, we publish fewer.</li>
        <li>Present an AI-generated image as a photograph of a real event.</li>
      </ul>

      <h2>Labels</h2>
      <p>
        Every article carries a visible label. <strong>News</strong> is evidence-based
        reporting with no opinion in it. <strong>Analysis</strong> interprets the evidence.
        <strong> Opinion</strong> and <strong>Commentary</strong> argue a position, and say
        so. Commercial content containing affiliate links lives in its own section and is
        disclosed.
      </p>

      <h2>Corrections</h2>
      <p>
        We get things wrong sometimes. When we do, we correct it publicly and permanently —
        corrections appear on the article itself and on our{" "}
        <Link href="/corrections">corrections page</Link>. If you have spotted an error,{" "}
        <Link href="/contact">tell us</Link>.
      </p>

      <p>
        More detail on our standards is in the{" "}
        <Link href="/editorial-policy">editorial policy</Link>.
      </p>
    </PageShell>
  );
}
