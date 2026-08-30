import type { Metadata } from "next";

import { DropLockup } from "@/components/brand/DropMark";
import { LoginForm } from "@/components/admin/LoginForm";

export const metadata: Metadata = {
  title: "Sign in",
  robots: { index: false, follow: false },
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;

  // Only same-site paths are accepted as a post-login redirect. Anything else is an
  // open-redirect vector, so it is discarded rather than sanitised.
  const redirectTo = next && next.startsWith("/admin") && !next.startsWith("//") ? next : "/admin";

  return (
    <div className="grid min-h-dvh place-items-center bg-bg px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <DropLockup markSize={32} />
        </div>
        <div className="rounded-lg border border-line bg-surface p-6" style={{ boxShadow: "var(--shadow-md)" }}>
          <h1 className="headline text-xl">Sign in</h1>
          <p className="dek mt-1 text-sm">Staff access only.</p>
          <LoginForm redirectTo={redirectTo} />
        </div>
      </div>
    </div>
  );
}
