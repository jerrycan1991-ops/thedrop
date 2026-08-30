import type { Metadata } from "next";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Contact",
  description: `How to reach ${SITE.name} — corrections, tips, rights requests and press.`,
  alternates: { canonical: `${SITE.url}/contact` },
};

const CONTACTS = [
  {
    label: "Corrections and errors",
    detail: "Point us at the article and the specific claim. We respond within two business days.",
    address: "corrections@thedrop.channel",
  },
  {
    label: "Rights, takedowns and copyright",
    detail: "Include the URL, the material in question, and the basis of the claim.",
    address: "rights@thedrop.channel",
  },
  {
    label: "Tips",
    detail: "Documents and first-hand information. We verify before we publish.",
    address: "tips@thedrop.channel",
  },
  {
    label: "Press and partnerships",
    detail: "Syndication, licensing and commercial enquiries.",
    address: "hello@thedrop.channel",
  },
];

export default function ContactPage() {
  return (
    <PageShell
      title="Contact"
      intro="Corrections take priority. Everything else, we get to in order."
    >
      <dl className="not-prose space-y-6">
        {CONTACTS.map((contact) => (
          <div key={contact.address} className="rounded-lg border border-line bg-surface p-5">
            <dt className="headline text-base">{contact.label}</dt>
            <dd className="dek mt-1 text-sm">{contact.detail}</dd>
            <dd className="mt-3">
              <a
                href={`mailto:${contact.address}`}
                className="text-sm font-medium text-accent-fg underline underline-offset-2"
              >
                {contact.address}
              </a>
            </dd>
          </div>
        ))}
      </dl>

      <p className="mt-8 text-sm text-subtle">
        These addresses must be provisioned before launch — see the deployment checklist.
      </p>
    </PageShell>
  );
}
