import "server-only";

import { cookies } from "next/headers";

import { validateSession } from "@/lib/auth/session";

/**
 * Shared plumbing for admin server components that read the database directly.
 *
 * Every one of these pages needs the same three things: validate the session, load
 * data, and say something useful when the database is down rather than throwing a
 * stack trace at an operator. Repeating that in each page is how the third one ends up
 * subtly different from the first two.
 *
 * Session validation is the same `validateSession` the API routes use — the TTL slide
 * and epoch check included. RBAC stays on the API routes: these pages are already
 * behind the admin gate, and rendering is not the place to duplicate an authorization
 * rule (see the note on the dashboard).
 */
export async function loadForAdmin<T>(
  load: () => Promise<T>,
  context: string,
): Promise<{ data: T } | { error: string }> {
  const sessionId = (await cookies()).get("thedrop_session")?.value;
  const session = await validateSession(sessionId);

  if (!session.ok) {
    return { error: "Session expired. Sign in again." };
  }

  try {
    return { data: await load() };
  } catch (error) {
    // Logged with context because "cannot load" on its own tells an operator nothing
    // about which query failed.
    console.error(`[admin] ${context} unavailable`, error);
    return { error: "Cannot load this data. Is the database reachable?" };
  }
}

export function AdminError({ title, message }: { title: string; message: string }) {
  return (
    <div className="p-8">
      <h1 className="display text-2xl">{title}</h1>
      <p className="mt-6 rounded-md border border-danger/30 bg-danger-subtle px-4 py-3 text-sm text-danger">
        {message}
      </p>
    </div>
  );
}

export function AdminHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="mb-8">
      <h1 className="display text-2xl">{title}</h1>
      {subtitle && <p className="meta mt-1">{subtitle}</p>}
    </header>
  );
}

/** Compact "3m ago" for operational screens, where absolute times read as noise. */
export function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(`${iso}+00:00`).getTime()) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
