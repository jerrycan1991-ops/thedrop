"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function LoginForm({ redirectTo }: { redirectTo: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);

    const form = new FormData(event.currentTarget);

    try {
      const response = await fetch("/api/v1/admin/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The session cookie is httpOnly and set by the API; the browser stores it
        // and this code never sees it.
        credentials: "same-origin",
        body: JSON.stringify({
          email: form.get("email"),
          password: form.get("password"),
        }),
      });

      if (!response.ok) {
        // The API returns one generic message for bad credentials so the response
        // cannot be used to enumerate accounts. Surface it verbatim.
        const body = await response.json().catch(() => ({}));
        setError(body.detail ?? "Sign in failed");
        setPending(false);
        return;
      }

      router.push(redirectTo);
      router.refresh();
    } catch {
      setError("Could not reach the server. Is the API running?");
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mt-6 space-y-4">
      <div>
        <label htmlFor="email" className="meta mb-1.5 block">
          Email
        </label>
        <input
          id="email"
          name="email"
          type="email"
          required
          autoComplete="username"
          className="h-10 w-full rounded-md border border-line bg-bg px-3 text-sm text-fg"
        />
      </div>

      <div>
        <label htmlFor="password" className="meta mb-1.5 block">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          required
          autoComplete="current-password"
          className="h-10 w-full rounded-md border border-line bg-bg px-3 text-sm text-fg"
        />
      </div>

      {error && (
        <p role="alert" className="rounded-md bg-danger-subtle px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="h-10 w-full rounded-md bg-accent text-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
