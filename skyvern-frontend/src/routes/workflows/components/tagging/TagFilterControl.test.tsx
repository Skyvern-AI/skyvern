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
  value?: Array<TagFilterTerm>;
  onChange?: (terms: Array<TagFilterTerm>) => void;
}) {
  return render(
    <MemoryRouter>
      <TagFilterControl
        tagKeys={tagKeys}
        value={props.value ?? []}
        onChange={props.onChange ?? (() => {})}
        labelSuggestions={["adhoc"]}
      />
    </MemoryRouter>,
  );
}

function openAndType(query: string) {
  fireEvent.click(screen.getByRole("button", { name: /tags/i }));
  const input = screen.getByPlaceholderText(/filter by/i);
  fireEvent.change(input, { target: { value: query } });
}

// All surfaces (workflows list + analytics dashboard) accept the same three
// term shapes: bare label, group:* (group-any), and exact group:value.
describe("TagFilterControl", () => {
  it("offers a bare label as an addable filter", () => {
    renderControl({});
    openAndType("prod");
    expect(screen.getByText(/Filter by label/)).toBeTruthy();
  });

  it("adds a bare label on selection", () => {
    const onChange = vi.fn();
    renderControl({ onChange });
    openAndType("prod");
    fireEvent.click(screen.getByText(/Filter by/));
    expect(onChange).toHaveBeenCalledWith([{ key: null, value: "prod" }]);
  });

  it("offers and adds an exact group:value", () => {
    const onChange = vi.fn();
    renderControl({ onChange });
    openAndType("env:prod");
    expect(screen.getByText(/env: prod/)).toBeTruthy();
    fireEvent.click(screen.getByText(/Filter by/));
    expect(onChange).toHaveBeenCalledWith([{ key: "env", value: "prod" }]);
  });

  it("adds a group-any term from a group suggestion", () => {
    const onChange = vi.fn();
    renderControl({ onChange });
    openAndType("env");
    fireEvent.click(screen.getByText(/: any/));
    expect(onChange).toHaveBeenCalledWith([{ key: "env", value: null }]);
  });
});
