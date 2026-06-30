// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const { getSpy } = vi.hoisted(() => ({ getSpy: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    get: (url: string) => {
      getSpy(url);
      return Promise.resolve({ data: null });
    },
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

import { HeroScreenshot, type HeroSelection } from "./HeroScreenshot";

function renderHero(selection: HeroSelection | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <HeroScreenshot selection={selection} running={false} />
    </QueryClientProvider>,
  );
}

beforeEach(() => getSpy.mockReset());
afterEach(cleanup);

describe("HeroScreenshot selection", () => {
  test("fetches the action's own screenshot artifact when an artifactId is present", async () => {
    renderHero({
      kind: "action",
      artifactId: "art_1",
      stepId: "stp_1",
      actionOrder: 0,
    });
    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(1));
    expect(getSpy.mock.calls[0]![0]).toContain("/artifacts/art_1");
  });

  test("falls back to the step's action screenshots when the action has no artifactId", async () => {
    renderHero({
      kind: "action",
      artifactId: null,
      stepId: "stp_1",
      actionOrder: 0,
    });
    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(1));
    expect(getSpy.mock.calls[0]![0]).toContain("/step/stp_1/artifacts");
  });

  test("fetches block artifacts for a block selection", async () => {
    renderHero({
      kind: "block",
      workflowRunBlockId: "wrb_1",
      blockType: "task",
    });
    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(1));
    expect(getSpy.mock.calls[0]![0]).toContain(
      "/workflow_run_block/wrb_1/artifacts",
    );
  });

  test("fetches thought artifacts for a thought selection", async () => {
    renderHero({ kind: "thought", thoughtId: "tht_1" });
    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(1));
    expect(getSpy.mock.calls[0]![0]).toContain("/thought/tht_1/artifacts");
  });

  test("renders the empty state and fetches nothing without a selection", () => {
    renderHero(null);
    screen.getByText("No screenshot for this selection.");
    expect(getSpy).not.toHaveBeenCalled();
  });
});
