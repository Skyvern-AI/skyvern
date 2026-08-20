// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("hides system tags when hideSystemTags is set, and renders nothing when only system tags remain", () => {
    const { container } = render(
      <TagChipList
        tags={[
          { key: "skyvern.platform", value: "browser" },
          { key: "env", value: "prod" },
        ]}
        hideSystemTags
      />,
    );
    expect(screen.getByText("prod")).toBeTruthy();
    expect(container.textContent).not.toContain("skyvern.platform");

    const { container: systemOnly } = render(
      <TagChipList
        tags={[{ key: "skyvern.platform", value: "browser" }]}
        hideSystemTags
      />,
    );
    expect(systemOnly.textContent).toBe("");
  });

  it("sorts system tags after user tags and renders them muted", () => {
    const { container } = render(
      <TagChipList
        tags={[
          { key: "skyvern.platform", value: "browser" },
          { key: "team", value: "growth" },
        ]}
        maxVisible={1}
      />,
    );
    // The single visible slot goes to the user tag; the system tag overflows
    // into the +1 badge.
    expect(screen.getByText("growth")).toBeTruthy();
    expect(screen.getByText("+1")).toBeTruthy();

    const { container: bothVisible } = render(
      <TagChipList
        tags={[{ key: "skyvern.platform", value: "browser" }]}
        maxVisible={2}
      />,
    );
    const systemChip = bothVisible.querySelector("span");
    expect(systemChip?.className).toContain("text-muted-foreground");
    expect(systemChip?.className).toContain("bg-transparent");
    expect(container.textContent).not.toContain("browser");
  });

  it("compact renders single-line small chips including the overflow badge", () => {
    const { container } = render(
      <TagChipList
        tags={[
          { key: null, value: "a" },
          { key: null, value: "b" },
        ]}
        maxVisible={1}
        compact
      />,
    );
    expect(container.firstElementChild?.className).toContain("flex-nowrap");
    const chip = screen.getByText("a").parentElement;
    expect(chip?.className).toContain("h-5");
    const overflow = screen.getByText("+1");
    expect(overflow.className).toContain("h-5");
  });

  it("only offers removal for user-editable tags", () => {
    const onRemove = vi.fn();
    render(
      <TagChipList
        tags={[
          { key: "env", value: "prod" },
          { key: "skyvern.platform", value: "browser" },
        ]}
        onRemove={onRemove}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove env: prod" }));

    expect(onRemove).toHaveBeenCalledWith({ key: "env", value: "prod" });
    expect(
      screen.queryByRole("button", {
        name: "Remove skyvern.platform: browser",
      }),
    ).toBeNull();
  });
});
