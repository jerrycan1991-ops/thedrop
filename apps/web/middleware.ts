import { NextResponse, type NextRequest } from "next/server";

/**
 * Admin gate.
 *
 * This is the cheap first check, not the security boundary. It rejects requests with
 * no session cookie at the edge so unauthenticated traffic never reaches a render.
 * The real authorization happens in FastAPI, which validates the session server-side
 * against Redis and enforces RBAC per route — a forged cookie gets past this
 * middleware and straight into a 401 from the API.
 *
 * Doing it in this order keeps the admin off the public process's hot path without
 * pretending a cookie's presence means anything.
 */
const SESSION_COOKIE = "thedrop_session";

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname === "/admin/login") {
    return NextResponse.next();
  }

  if (pathname.startsWith("/admin")) {
    const session = request.cookies.get(SESSION_COOKIE);

    if (!session?.value) {
      const loginUrl = new URL("/admin/login", request.url);
      loginUrl.searchParams.set("next", pathname);
      return NextResponse.redirect(loginUrl);
    }

    // The admin is never indexed, and never framed.
    const response = NextResponse.next();
    response.headers.set("X-Robots-Tag", "noindex, nofollow, noarchive");
    response.headers.set("X-Frame-Options", "DENY");
    response.headers.set("Cache-Control", "no-store, must-revalidate");
    return response;
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/admin/:path*"],
};
