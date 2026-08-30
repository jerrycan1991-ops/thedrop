import type { Metadata } from "next";
import Link from "next/link";

import { SITE } from "@thedrop/config";

import { PageShell } from "@/components/site/PageShell";

export const metadata: Metadata = {
  title: "Newsletter",
  description: `One email each morning from ${SITE.name}.`,
  alternates: { canonical: `${SITE.url}/newsletter` },
};

export default function NewsletterPage() {
  return (
    <PageShell
      title="The Drop, daily"
      intro="What happened, what it means, and what is still unconfirmed. One email, each morning."
    >
      {/* Posts to the API, which stores the subscriber in our own table with double
          opt-in. The list is ours and portable from day one -- a list locked inside a
          vendor is a liability (MONETIZATION.md §6). */}
      <form
        action="/api/v1/public/newsletter/subscribe"
        method="post"
        className="not-prose mt-2 flex flex-col gap-3 sm:flex-row"
      >
        <label htmlFor="email" className="sr-only">
          Email address
        </label>
        <input
          id="email"
          type="email"
          name="email"
          required
          autoComplete="email"
          placeholder="you@example.com"
          className="h-11 flex-1 rounded-md border border-line bg-surface px-4 text-base text-fg placeholder:text-subtle"
        />
        <button
          type="submit"
          className="h-11 rounded-md bg-accent px-6 text-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover"
        >
          Subscribe
        </button>
      </form>

      <p className="mt-4 text-sm text-subtle">
        Double opt-in — we send one confirmation email and nothing else until you click it.
        One-click unsubscribe in every issue. We never sell your address. See our{" "}
        <Link href="/privacy">privacy policy</Link>.
      </p>

      <p className="mt-8 text-sm text-subtle">
        Sending is enabled in a later phase; subscriptions collected now are stored and
        confirmed, not mailed.
      </p>
    </PageShell>
  );
}
