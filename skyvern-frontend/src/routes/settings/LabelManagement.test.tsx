// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { LabelManagement } from "./LabelManagement";
import type { TagValue } from "@/routes/workflows/types/tagTypes";

// Radix Popover/Dialog and cmdk touch DOM APIs jsdom lacks.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;
Element.prototype.scrollIntoView = () => {};
Element.prototype.hasPointerCapture = () => false;
Element.prototype.releasePointerCapture = () => {};

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const deleteMock = vi.fn();

vi.mock("@/api/AxiosClient", () => ({
  getClient: () =>
    Promise.resolve({
      get: getMock,
      post: postMock,
      patch: patchMock,
      delete: deleteMock,
    }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const tagValues: Array<TagValue> = [
  { key: "env", value: "prod", color: "blue", workflow_count: 3 },
  { key: "env", value: "staging", color: "amber", workflow_count: 1 },
  { key: "team", value: "growth", color: "green", workflow_count: 0 },
];

beforeEach(() => {
  getMock.mockResolvedValue({ data: tagValues });
  postMock.mockResolvedValue({ data: {} });
  patchMock.mockResolvedValue({ data: {} });
  deleteMock.mockResolvedValue({ data: {} });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSurface() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <LabelManagement />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LabelManagement", () => {
  it("groups labels by key and shows per-label usage counts", async () => {
    renderSurface();

    expect(await screen.findByRole("heading", { name: "env" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "team" })).toBeTruthy();
    expect(screen.getByText("3 agents")).toBeTruthy();
    expect(screen.getByText("1 agent")).toBeTruthy();
    expect(screen.getByText("0 agents")).toBeTruthy();
  });

  it("shows an empty state when no labels are registered", async () => {
    getMock.mockResolvedValue({ data: [] });
    renderSurface();

    expect(await screen.findByText(/No labels yet/)).toBeTruthy();
  });

  it("deletes a label with a cascade blast-radius confirm", async () => {
    renderSurface();

    fireEvent.click(await screen.findByLabelText("Delete prod"));

    expect(screen.getByText(/Delete label/)).toBeTruthy();
    expect(
      screen.getByText(/removes it from 3 agents and from the/),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(deleteMock).toHaveBeenCalledWith("/tag-values/env", {
        data: { value: "prod" },
      });
    });
  });

  it("renames a label, sending the value in the body", async () => {
    renderSurface();

    fireEvent.click(await screen.findByLabelText("Rename prod"));
    const input = screen.getByLabelText("Rename label prod");
    fireEvent.change(input, { target: { value: "production" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith("/tag-values/env/rename", {
        value: "prod",
        new_value: "production",
      });
    });
  });

  it("surfaces a clear inline error when a rename collides (409)", async () => {
    patchMock.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 409, data: { detail: "exists" } },
    });
    renderSurface();

    fireEvent.click(await screen.findByLabelText("Rename staging"));
    const input = screen.getByLabelText("Rename label staging");
    fireEvent.change(input, { target: { value: "prod" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(
        "A label with that name already exists in this group.",
      ),
    ).toBeTruthy();
  });

  it("recolors a label via the swatch picker", async () => {
    renderSurface();

    fireEvent.click(await screen.findByLabelText("Change color for prod"));
    fireEvent.click(await screen.findByLabelText("red"));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith("/tag-values/env", {
        value: "prod",
        color: "red",
      });
    });
  });

  it("hides reserved skyvern.* rows from the label list entirely", async () => {
    getMock.mockResolvedValue({
      data: [
        ...tagValues,
        {
          key: "skyvern.platform",
          value: "web",
          color: "blue",
          workflow_count: 2,
        },
      ],
    });
    renderSurface();

    expect(await screen.findByRole("heading", { name: "env" })).toBeTruthy();
    expect(
      screen.queryByRole("heading", { name: "skyvern.platform" }),
    ).toBeNull();
    expect(screen.queryByText(/skyvern\.platform/)).toBeNull();
    // Non-reserved rows are unaffected.
    expect(screen.getByLabelText("Rename prod")).toBeTruthy();
  });

  it("shows the empty state when only reserved labels exist", async () => {
    getMock.mockResolvedValue({
      data: [
        {
          key: "skyvern.platform",
          value: "web",
          color: "blue",
          workflow_count: 2,
        },
      ],
    });
    renderSurface();

    expect(await screen.findByText(/No labels yet/)).toBeTruthy();
  });

  it("renders rows with value-only chips, without the group prefix", async () => {
    renderSurface();

    await screen.findByRole("heading", { name: "env" });
    // The group name lives only in the section heading (h3), never in the chips.
    expect(screen.queryByText("env", { selector: "span" })).toBeNull();
    expect(screen.getByText("prod")).toBeTruthy();
  });

  it("filters rows by label value as you type", async () => {
    renderSurface();

    await screen.findByRole("heading", { name: "env" });
    fireEvent.change(screen.getByLabelText("Search labels"), {
      target: { value: "grow" },
    });

    expect(screen.queryByRole("heading", { name: "env" })).toBeNull();
    expect(screen.getByRole("heading", { name: "team" })).toBeTruthy();
    expect(screen.getByText("growth")).toBeTruthy();
  });

  it("keeps a whole group visible when the query matches its name", async () => {
    renderSurface();

    await screen.findByRole("heading", { name: "env" });
    fireEvent.change(screen.getByLabelText("Search labels"), {
      target: { value: "env" },
    });

    expect(screen.getByText("prod")).toBeTruthy();
    expect(screen.getByText("staging")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "team" })).toBeNull();
  });

  it("shows a no-match state for an unmatched query", async () => {
    renderSurface();

    await screen.findByRole("heading", { name: "env" });
    fireEvent.change(screen.getByLabelText("Search labels"), {
      target: { value: "zzz" },
    });

    expect(screen.getByText(/No labels match/)).toBeTruthy();
  });

  it("creates a label from the New label dialog", async () => {
    postMock.mockResolvedValue({
      data: { key: "region", value: "emea", color: "teal", workflow_count: 0 },
    });
    renderSurface();

    fireEvent.click(await screen.findByRole("button", { name: "New label" }));
    fireEvent.change(screen.getByLabelText("Group"), {
      target: { value: "region" },
    });
    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "emea" },
    });
    fireEvent.click(screen.getByLabelText("teal"));
    fireEvent.click(screen.getByRole("button", { name: "Create label" }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith("/tag-values", {
        key: "region",
        value: "emea",
        color: "teal",
      });
    });
  });

  it("rejects a reserved group inline in the create dialog", async () => {
    renderSurface();

    fireEvent.click(await screen.findByRole("button", { name: "New label" }));
    fireEvent.change(screen.getByLabelText("Group"), {
      target: { value: "skyvern.platform" },
    });
    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "web" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create label" }));

    expect(await screen.findByText(/reserved/)).toBeTruthy();
    expect(postMock).not.toHaveBeenCalled();
  });

  it("offers a New label CTA in the empty state", async () => {
    getMock.mockResolvedValue({ data: [] });
    renderSurface();

    expect(await screen.findByText(/No labels yet/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "New label" })).toBeTruthy();
  });
});
