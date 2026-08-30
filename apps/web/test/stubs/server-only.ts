/**
 * Test stub for the `server-only` package.
 *
 * The real package throws unless imported under React's `react-server` condition,
 * which Vitest does not set. Aliasing it here lets server-side modules be unit-tested
 * without weakening anything: the genuine guard is enforced by every `next build`,
 * and a client component importing a server module still fails there.
 */
export {};
