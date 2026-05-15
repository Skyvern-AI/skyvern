// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { storage } = vi.hoisted(() => {
  const storage = new Map<string, string>();
  const localStorageMock = {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storage.set(key, value);
    },
    removeItem: (key: string) => {
      storage.delete(key);
    },
    clear: () => {
      storage.clear();
    },
    key: (index: number) => Array.from(storage.keys())[index] ?? null,
    get length() {
      return storage.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: localStorageMock,
    configurable: true,
    writable: true,
  });
  return { storage };
});

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    get: async () => ({ data: null }),
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

const { isViewingV2Mock } = vi.hoisted(() => ({
  isViewingV2Mock: vi.fn(() => false),
}));

vi.mock("@/hooks/useWorkflowRunViewingV2", () => ({
  useWorkflowRunViewingV2: () => isViewingV2Mock(),
}));

import { Action, ActionTypes } from "@/api/types";
import {
  RUN_VIEWING_STORAGE_KEY,
  useRunViewingPreferenceStore,
} from "@/store/RunViewingPreferenceStore";
import { ScrollableActionList } from "./ScrollableActionList";

function buildAction(overrides: Partial<Action> = {}): Action {
  return {
    reasoning: "Click the submit button",
    confidence: 0.9,
    type: ActionTypes.Click,
    input: "",
    success: true,
    stepId: "step_1",
    index: 0,
    created_by: null,
    screenshotArtifactId: null,
    ...overrides,
  };
}

function renderList(extraProps?: { activeIndex?: number | "stream" }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ScrollableActionList
        data={[buildAction()]}
        activeIndex={extraProps?.activeIndex ?? 0}
        onActiveIndexChange={() => {}}
        showStreamOption={false}
        taskDetails={{ actions: 1, steps: 1 }}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  storage.clear();
  useRunViewingPreferenceStore.setState({ viewMode: "compact" }, false);
  isViewingV2Mock.mockReset();
  isViewingV2Mock.mockReturnValue(false);
});

afterEach(() => {
  cleanup();
});

describe("ScrollableActionList view-mode swap", () => {
  it("renders the legacy detailed card when the v2 flag is off", () => {
    renderList();

    expect(screen.queryByLabelText("Compact view")).toBeNull();
    expect(screen.queryByLabelText("Detailed view")).toBeNull();
    expect(
      document.querySelector('[data-slot="action-card-compact"]'),
    ).toBeNull();
    // getByText throws if not found, so reaching this line means it rendered
    screen.getByText("Click the submit button");
  });

  it("renders compact cards and the toggle when v2 flag on + viewMode=compact", () => {
    isViewingV2Mock.mockReturnValue(true);
    useRunViewingPreferenceStore.setState({ viewMode: "compact" }, false);

    renderList();

    // getByLabelText throws if not found
    screen.getByLabelText("Compact view");
    screen.getByLabelText("Detailed view");
    expect(
      document.querySelector('[data-slot="action-card-compact"]'),
    ).not.toBeNull();
    // sanity: persistence path is wired
    expect(RUN_VIEWING_STORAGE_KEY).toBe("skyvern.runViewing");
  });
});
