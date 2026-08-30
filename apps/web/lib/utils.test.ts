import { describe, expect, it } from "vitest";

import { formatReadingTime, relativeTime } from "./utils";

describe("formatReadingTime", () => {
  it("rounds to whole minutes", () => {
    expect(formatReadingTime(240)).toBe("4 min read");
  });

  it("never reports zero minutes", () => {
    // A 20-second read is still "1 min read"; "0 min read" reads as broken.
    expect(formatReadingTime(20)).toBe("1 min read");
    expect(formatReadingTime(0)).toBe("1 min read");
  });
});

describe("relativeTime", () => {
  it("returns an empty string for a missing timestamp", () => {
    expect(relativeTime(null)).toBe("");
  });

  it("describes the last hour in minutes", () => {
    const tenMinutesAgo = new Date(Date.now() - 10 * 60_000).toISOString();
    expect(relativeTime(tenMinutesAgo)).toBe("10m ago");
  });

  it("describes the last day in hours", () => {
    const threeHoursAgo = new Date(Date.now() - 3 * 3_600_000).toISOString();
    expect(relativeTime(threeHoursAgo)).toBe("3h ago");
  });

  it("falls back to an absolute date beyond a week", () => {
    const longAgo = new Date("2020-01-15T12:00:00Z").toISOString();
    expect(relativeTime(longAgo)).toMatch(/2020/);
  });
});
