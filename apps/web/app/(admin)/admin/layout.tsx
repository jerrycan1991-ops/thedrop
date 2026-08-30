/**
 * Applies to everything under /admin, including the login page.
 *
 * The sidebar shell lives in the (shell) route group instead, so the login screen
 * renders without navigation the visitor cannot use yet.
 */

// Admin responses are per-user and must never be cached or prerendered.
export const dynamic = "force-dynamic";

export const metadata = {
  title: { default: "Admin", template: "%s | The Drop Admin" },
  robots: { index: false, follow: false },
};

export default function AdminRootLayout({ children }: { children: React.ReactNode }) {
  return children;
}
