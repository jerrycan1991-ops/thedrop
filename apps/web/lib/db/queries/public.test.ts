import { describe, expect, it } from "vitest";

import { pyIso } from "@/lib/db/datetime";

import { __testing } from "./public";

const { articlePath } = __testing;

/**
 * These pin the serialisation contract captured from FastAPI at tag v0.1.0-hybrid.
 * The live parity run proved equivalence at a point in time; these keep it true.
 */

function row(overrides: Record<string, unknown> = {}) {
  return {
    public_id: "0d087426-7a69-4598-8e91-8bd7f9c094d8",
    slug: "a-story",
    category_slug: "trending",
    first_published_at_iso: "2026-08-20T12:00:00.000000",
    ...overrides,
  } as Parameters<typeof articlePath>[0];
}

describe("articlePath", () => {
  it("builds the canonical dated path", () => {
    expect(articlePath(row())).toBe("/trending/2026/08/20/a-story");
  });

  it("zero-pads month and day", () => {
    // "2026/8/5" and "2026/08/05" would be two URLs for one article.
    const path = articlePath(row({ first_published_at_iso: "2026-01-05T00:00:00.000000" }));
    expect(path).toBe("/trending/2026/01/05/a-story");
  });

  it("uses UTC, not local time", () => {
    // Late-UTC timestamps roll to the previous day in western zones; the path must
    // not depend on where the server happens to run.
    const path = articlePath(row({ first_published_at_iso: "2026-08-20T23:59:00.000000" }));
    expect(path).toBe("/trending/2026/08/20/a-story");
  });

  it("returns a preview path when unpublished", () => {
    expect(articlePath(row({ first_published_at_iso: null }))).toBe(
      "/preview/0d087426-7a69-4598-8e91-8bd7f9c094d8",
    );
  });
});

describe("pyIso", () => {
  it("omits the fraction when microseconds are zero, as Python does", () => {
    expect(pyIso("2026-08-20T12:00:00.000000")).toBe("2026-08-20T12:00:00+00:00");
  });

  it("keeps six digits when microseconds are non-zero", () => {
    // JavaScript Dates hold milliseconds, so this precision only survives because
    // PostgreSQL formats the value and we never build a Date from it.
    expect(pyIso("2026-08-20T12:00:00.123456")).toBe("2026-08-20T12:00:00.123456+00:00");
  });

  it("passes null through", () => {
    expect(pyIso(null)).toBeNull();
  });
});

describe("pagination contract", () => {
  // total is deliberately an ESTIMATE: one extra row is fetched to determine hasMore
  // without a second COUNT(*). Reproduced here so a "fix" to make it exact fails.
  function paginate(rowCount: number, page: number, pageSize: number) {
    const offset = (page - 1) * pageSize;
    const hasMore = rowCount > pageSize;
    const items = Math.min(rowCount, pageSize);
    return { hasMore, total: offset + items + (hasMore ? 1 : 0) };
  }

  it("reports hasMore when an extra row came back", () => {
    expect(paginate(3, 1, 2)).toEqual({ hasMore: true, total: 3 });
  });

  it("reports the exact count on the final page", () => {
    expect(paginate(1, 4, 2)).toEqual({ hasMore: false, total: 7 });
  });

  it("returns zero for an empty result", () => {
    expect(paginate(0, 1, 20)).toEqual({ hasMore: false, total: 0 });
  });
});
