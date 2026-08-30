import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: `What ${SITE.name} collects, why, and what we refuse to do with it.`,
  alternates: { canonical: `${SITE.url}/privacy` },
};

export default function PrivacyPage() {
  return (
    <PageShell
      title="Privacy Policy"
      intro="Short version: we measure what we publish, not who you are."
      updated="August 2026"
    >
      <h2>What we collect</h2>
      <ul>
        <li>
          <strong>Analytics.</strong> Page views, how far down an article people read, how
          long they stay, and which site referred them. This is first-party — it stays on
          our servers. IP addresses are truncated before storage and are not used to build
          a profile.
        </li>
        <li>
          <strong>Newsletter.</strong> If you subscribe, we store your email address, when
          you confirmed, and your preferences. Nothing else.
        </li>
        <li>
          <strong>Contact.</strong> If you email us, we keep the message so we can act on it.
        </li>
      </ul>

      <h2>What we do not do</h2>
      <ul>
        <li>No third-party behavioural trackers.</li>
        <li>No cross-site identifiers or fingerprinting.</li>
        <li>No selling or renting your data. Ever.</li>
        <li>No account required to read.</li>
      </ul>

      <h2>Cookies</h2>
      <p>
        The public site sets no advertising or tracking cookies. Your theme preference is
        stored in your browser&rsquo;s local storage and never leaves your device. Staff
        logins use a strictly-necessary session cookie on the admin area only.
      </p>
      <p>
        If we introduce advertising, this page will be updated before any ad loads, and
        consent will be requested where the law requires it.
      </p>

      <h2>Outbound links</h2>
      <p>
        Product links in our commercial content pass through our own redirect so we can
        count clicks. The retailer you land on has its own privacy practices, which we do
        not control. See our <Link href="/affiliate-disclosure">affiliate disclosure</Link>.
      </p>

      <h2>Retention</h2>
      <p>
        Raw analytics events are deleted after 35 days; only aggregates are kept.
        Newsletter records are kept until you unsubscribe, then removed.
      </p>

      <h2>Your rights</h2>
      <p>
        You can ask for a copy of the data we hold about you, or ask us to delete it.
        Every newsletter has a one-click unsubscribe. <Link href="/contact">Contact us</Link>{" "}
        and we will action it.
      </p>
    </PageShell>
  );
}
