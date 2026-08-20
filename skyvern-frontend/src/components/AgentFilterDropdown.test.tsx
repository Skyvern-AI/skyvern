// @vitest-environment jsdom
import { useState, type ReactNode } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentFilterDropdown } from "./AgentFilterDropdown";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;
Element.prototype.scrollIntoView = () => {};

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("use-debounce", () => ({
  useDebounce: <T,>(value: T): [T] => [value],
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ get: getMock })),
}));

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function ControlledDropdown({
  onChange,
}: {
  onChange: (values: Array<string>) => void;
}) {
  const [values, setValues] = useState<Array<string>>([]);
  return (
    <AgentFilterDropdown
      values={values}
      onChange={(nextValues) => {
        onChange(nextValues);
        setValues(nextValues);
      }}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentFilterDropdown", () => {
  it("shows fetched agents when opened", async () => {
    getMock.mockResolvedValue({
      data: [
        { title: "Agent One", workflow_permanent_id: "wpid_1" },
        { title: "Agent Two", workflow_permanent_id: "wpid_2" },
      ],
    });

    render(<AgentFilterDropdown values={[]} onChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));

    expect(await screen.findByText("Agent One")).toBeTruthy();
    expect(screen.getByText("Agent Two")).toBeTruthy();
  });

  it("adds and removes an agent without closing the popover", async () => {
    getMock.mockResolvedValue({
      data: [
        { title: "Agent One", workflow_permanent_id: "wpid_1" },
        { title: "Agent Two", workflow_permanent_id: "wpid_2" },
      ],
    });
    const onChange = vi.fn();

    render(<ControlledDropdown onChange={onChange} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));

    const title = await screen.findByText("Agent One");
    fireEvent.click(title);
    expect(onChange).toHaveBeenLastCalledWith(["wpid_1"]);

    const selectedRow = title.closest("[cmdk-item]");
    expect(selectedRow).not.toBeNull();
    expect(
      within(selectedRow as HTMLElement)
        .getByRole("checkbox")
        .getAttribute("data-state"),
    ).toBe("checked");
    expect(screen.getByPlaceholderText("Search agents...")).toBeTruthy();

    fireEvent.click(title);
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.getByPlaceholderText("Search agents...")).toBeTruthy();
  });

  it("shows an error and retries loading agents", async () => {
    getMock.mockRejectedValue(new Error("Request failed"));

    render(<AgentFilterDropdown values={[]} onChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));

    expect(await screen.findByText("Failed to load agents.")).toBeTruthy();
    expect(screen.queryByText("No agents found.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(getMock.mock.calls.length).toBeGreaterThan(1);
    });
  });

  it("removes a selected agent that is missing from fetched results", async () => {
    getMock.mockResolvedValue({
      data: [{ title: "Agent One", workflow_permanent_id: "wpid_1" }],
    });
    const onChange = vi.fn();

    render(
      <AgentFilterDropdown
        values={["wpid_missing", "wpid_1"]}
        onChange={onChange}
      />,
      { wrapper },
    );
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));

    expect(await screen.findByText("Selected")).toBeTruthy();
    fireEvent.click(screen.getByText("wpid_missing"));

    expect(onChange).toHaveBeenCalledWith(["wpid_1"]);
  });

  it("clears all selected agents", async () => {
    getMock.mockResolvedValue({
      data: [{ title: "Agent One", workflow_permanent_id: "wpid_1" }],
    });
    const onChange = vi.fn();

    render(
      <AgentFilterDropdown
        values={["wpid_1", "wpid_missing"]}
        onChange={onChange}
      />,
      { wrapper },
    );
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));

    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("disables unchecked agents at the selection limit", async () => {
    getMock.mockResolvedValue({
      data: [
        { title: "Selected Agent", workflow_permanent_id: "wpid_1" },
        { title: "Unchecked Agent", workflow_permanent_id: "wpid_51" },
      ],
    });
    const selectedValues = Array.from(
      { length: 50 },
      (_, index) => `wpid_${index + 1}`,
    );

    render(<AgentFilterDropdown values={selectedValues} onChange={vi.fn()} />, {
      wrapper,
    });
    fireEvent.click(screen.getByRole("button", { name: /filter by agent/i }));

    const selectedRow = (await screen.findByText("Selected Agent")).closest(
      "[cmdk-item]",
    );
    const uncheckedRow = screen
      .getByText("Unchecked Agent")
      .closest("[cmdk-item]");

    expect(selectedRow).not.toBeNull();
    expect(uncheckedRow).not.toBeNull();
    expect(selectedRow?.getAttribute("data-disabled")).not.toBe("true");
    expect(uncheckedRow?.getAttribute("data-disabled")).toBe("true");
  });
});
