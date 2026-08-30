import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TypeBadge } from "./TypeBadge";

describe("TypeBadge", () => {
  it("labels news", () => {
    render(<TypeBadge type="NEWS" />);
    expect(screen.getByText("News")).toBeDefined();
  });

  it("labels opinion distinctly from news", () => {
    // The whole point of the badge: a reader must never mistake opinion for reporting.
    const { container: news } = render(<TypeBadge type="NEWS" />);
    const { container: opinion } = render(<TypeBadge type="OPINION" />);
    expect(news.firstElementChild?.className).not.toBe(
      opinion.firstElementChild?.className,
    );
  });

  it("labels commercial article types", () => {
    render(<TypeBadge type="PRODUCT_REVIEW" />);
    expect(screen.getByText("Product Review")).toBeDefined();
  });

  it("renders nothing for an unknown type", () => {
    // Better to show no label than to print a raw enum value at a reader.
    const { container } = render(<TypeBadge type="NOT_A_REAL_TYPE" />);
    expect(container.firstChild).toBeNull();
  });
});
