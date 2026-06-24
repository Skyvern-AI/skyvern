// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Tag } from "../../types/tagTypes";
import { buildTagColorMap } from "../../types/tagColors";
import { TagChipList } from "./TagChipList";
import { TagChip } from "./TagChip";

// Regression for the SKY-10683 backend/frontend shape split: a skewed tags
// payload reached the chips untyped, rendered a raw object child (React #31),
// and the route error boundary took down the whole /workflows page.

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TagChipList shape tolerance", () => {
  it("renders the valid subset when list entries are malformed", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const skewed = [
      { key: "env", value: "prod" },
      { key: "junk", value: { key: "nested", value: "object" } },
      null,
    ] as unknown as Array<Tag>;
    expect(() => render(<TagChipList tags={skewed} />)).not.toThrow();
    expect(screen.getByText("prod")).toBeTruthy();
    expect(screen.queryByText("junk")).toBeNull();
  });

  it("converts a legacy record payload instead of throwing", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const legacy = { env: "prod" } as unknown as Array<Tag>;
    expect(() => render(<TagChipList tags={legacy} />)).not.toThrow();
    expect(screen.getByText("prod")).toBeTruthy();
  });
});

describe("TagChip shape tolerance", () => {
  it("renders nothing and warns when value is not a string", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const objectValue = { key: "k", value: "v" } as unknown as string;
    const { container } = render(<TagChip tagKey="env" value={objectValue} />);
    expect(container.textContent).toBe("");
    expect(warn).toHaveBeenCalledTimes(1);
  });
});

describe("TagChip color", () => {
  it("renders a leading color dot for a grouped tag and keeps the surface neutral", () => {
    const { container } = render(
      <TagChip tagKey="env" value="prod" color="blue" />,
    );
    const chip = container.querySelector("span");
    expect(chip?.className).toContain("bg-badge-neutral");
    expect(chip?.className).not.toContain("bg-blue-100");
    const dot = container.querySelector(".rounded-full");
    expect(dot?.className).toContain("bg-blue-500");
  });

  it("renders no dot on a standalone label", () => {
    const { container } = render(
      <TagChip tagKey={null} value="prod" color="blue" />,
    );
    expect(container.querySelector(".rounded-full")).toBeNull();
    const chip = container.querySelector("span");
    expect(chip?.className).not.toContain("bg-blue-500");
  });

  it("renders no dot when the color is outside the palette", () => {
    const { container } = render(
      <TagChip tagKey="env" value="prod" color="chartreuse" />,
    );
    expect(container.querySelector(".rounded-full")).toBeNull();
    const chip = container.querySelector("span");
    expect(chip?.className).not.toContain("chartreuse");
    expect(chip?.className).toContain("bg-badge-neutral");
  });
});

describe("TagChipList colors", () => {
  it("shows a color dot on grouped chips and leaves labels neutral", () => {
    const colors = buildTagColorMap([
      { key: "env", value: "prod", color: "green" },
    ]);
    const { container } = render(
      <TagChipList
        tags={[
          { key: "env", value: "prod" },
          { key: null, value: "standalone" },
        ]}
        colors={colors}
      />,
    );
    const html = container.innerHTML;
    expect(html).toContain("bg-green-500");
    expect(screen.getByText("standalone")).toBeTruthy();
  });
});
