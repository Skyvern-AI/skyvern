// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { TagFilterControl } from "./TagFilterControl";
import type { TagFilterTerm, TagKey } from "../../types/tagTypes";

// cmdk uses ResizeObserver and scrollIntoView, which jsdom lacks.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;
Element.prototype.scrollIntoView = () => {};

afterEach(cleanup);

const tagKeys: Array<TagKey> = [
  { key: "env", description: "Environment", workflow_count: 3 },
];

function renderControl(props: {
  exactValuesOnly?: boolean;
  value?: Array<TagFilterTerm>;
  onChange?: (terms: Array<TagFilterTerm>) => void;
  tagKeys?: Array<TagKey>;
  onDeleteKey?: (tagKey: TagKey) => void;
}) {
  return render(
    <MemoryRouter>
      <TagFilterControl
        tagKeys={props.tagKeys ?? tagKeys}
        value={props.value ?? []}
        onChange={props.onChange ?? (() => {})}
        labelSuggestions={["adhoc"]}
        exactValuesOnly={props.exactValuesOnly}
        onDeleteKey={props.onDeleteKey}
      />
    </MemoryRouter>,
  );
}

function openAndType(query: string) {
  fireEvent.click(screen.getByRole("button", { name: /tags/i }));
  const input = screen.getByPlaceholderText(/filter by/i);
  fireEvent.change(input, { target: { value: query } });
}

describe("TagFilterControl exactValuesOnly", () => {
  it("does not offer a bare label as an addable filter", () => {
    renderControl({ exactValuesOnly: true });
    openAndType("prod");
    expect(screen.queryByText(/^Filter by/)).toBeNull();
  });

  it("offers an exact group:value as an addable filter", () => {
    renderControl({ exactValuesOnly: true });
    openAndType("env:prod");
    expect(screen.getByText(/env: prod/)).toBeTruthy();
  });

  it("adds an exact term on selection", () => {
    const onChange = vi.fn();
    renderControl({ exactValuesOnly: true, onChange });
    openAndType("env:prod");
    fireEvent.click(screen.getByText(/Filter by/));
    expect(onChange).toHaveBeenCalledWith([{ key: "env", value: "prod" }]);
  });
});

describe("TagFilterControl default mode", () => {
  it("still offers a bare label as an addable filter (workflows list parity)", () => {
    renderControl({ exactValuesOnly: false });
    openAndType("prod");
    expect(screen.getByText(/Filter by label/)).toBeTruthy();
  });

  it("keeps reserved system groups filterable but hides their delete action", () => {
    const onChange = vi.fn();
    const onDeleteKey = vi.fn();
    renderControl({
      onChange,
      onDeleteKey,
      tagKeys: [
        ...tagKeys,
        {
          key: "skyvern.platform",
          description: null,
          workflow_count: 1,
        },
      ],
    });

    openAndType("skyvern");

    expect(screen.getByText("skyvern.platform")).toBeTruthy();
    expect(screen.queryByLabelText("Delete group skyvern.platform")).toBeNull();

    fireEvent.click(screen.getByText("skyvern.platform"));
    expect(onChange).toHaveBeenCalledWith([
      { key: "skyvern.platform", value: null },
    ]);
    expect(onDeleteKey).not.toHaveBeenCalled();
  });
});
