// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ArtifactApiResponse, ArtifactType } from "@/api/types";

const { getSpy, mintMock, responses } = vi.hoisted(() => ({
  getSpy: vi.fn(),
  mintMock: vi.fn(),
  responses: new Map<string, unknown>(),
}));

vi.mock("@/api/artifactUrls", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/artifactUrls")>()),
  mintSignedArtifactUrl: mintMock,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    get: (url: string) => {
      getSpy(url);
      const key = [...responses.keys()].find((path) => url.endsWith(path));
      const response = key === undefined ? null : responses.get(key);
      if (response instanceof Error) {
        return Promise.reject(response);
      }
      return Promise.resolve(response).then((data) => ({ data }));
    },
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

import { HeroScreenshot, type HeroSelection } from "./HeroScreenshot";

function renderHero(
  selection: HeroSelection | null,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  running = false,
) {
  const view = (current: HeroSelection | null, isRunning: boolean) => (
    <QueryClientProvider client={client}>
      <HeroScreenshot selection={current} running={isRunning} />
    </QueryClientProvider>
  );
  const rendered = render(view(selection, running));
  return {
    ...rendered,
    setRunning: (isRunning: boolean) =>
      rendered.rerender(view(selection, isRunning)),
    setSelection: (next: HeroSelection) =>
      rendered.rerender(view(next, running)),
  };
}

function shot(
  id: string,
  type: ArtifactType,
  createdAt: string,
): ArtifactApiResponse {
  return {
    artifact_id: id,
    artifact_type: type,
    created_at: createdAt,
    modified_at: createdAt,
    task_id: "tsk_1",
    step_id: "stp_1",
    uri: `https://artifacts.test/${id}.png`,
    signed_url: `https://artifacts.test/${id}.png`,
    organization_id: "o_1",
  };
}

function watchForText(text: string) {
  const seen: string[] = [];
  const collect = (records: MutationRecord[]) =>
    records.forEach((record) => {
      seen.push(record.target.textContent ?? "");
      record.addedNodes.forEach((node) => seen.push(node.textContent ?? ""));
    });
  const observer = new MutationObserver(collect);
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  return () => {
    collect(observer.takeRecords());
    observer.disconnect();
    return seen.some((content) => content.includes(text));
  };
}

const blockStart = shot(
  "llm_block_start",
  ArtifactType.LLMScreenshot,
  "2026-10-30T10:00:00Z",
);
const firstAction = shot(
  "action_1",
  ArtifactType.ActionScreenshot,
  "2026-10-30T10:00:10Z",
);
const secondAction = shot(
  "action_2",
  ArtifactType.ActionScreenshot,
  "2026-10-30T10:00:20Z",
);
const atFailure = shot(
  "llm_at_failure",
  ArtifactType.LLMScreenshot,
  "2026-10-30T10:00:30Z",
);

const trailingFailedRow: HeroSelection = {
  kind: "action",
  artifactId: null,
  stepId: "stp_1",
  actionOrder: 2,
  workflowRunBlockId: "wrb_1",
};

beforeEach(() => {
  getSpy.mockReset();
  mintMock.mockReset();
  responses.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("HeroScreenshot selection", () => {
  test("fetches the action's own screenshot artifact when an artifactId is present", async () => {
    renderHero({
      kind: "action",
      artifactId: "art_1",
      stepId: "stp_1",
      actionOrder: 0,
      workflowRunBlockId: null,
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
      workflowRunBlockId: null,
    });
    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(1));
    expect(getSpy.mock.calls[0]![0]).toContain("/step/stp_1/artifacts");
  });

  test("a trailing failed row shows the block's at-failure screenshot", async () => {
    responses.set("/step/stp_1/artifacts", [secondAction, firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      atFailure,
      secondAction,
      firstAction,
      blockStart,
    ]);
    renderHero(trailingFailedRow);
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(atFailure.signed_url);
  });

  test("a trailing failed row without an at-failure capture shows the newest action screenshot, even from a stale step list", async () => {
    responses.set("/step/stp_1/artifacts", [firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      secondAction,
      firstAction,
      blockStart,
    ]);
    renderHero(trailingFailedRow);
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(secondAction.signed_url);
  });

  test("a trailing failed row never shows the block-start screenshot over an action screenshot", async () => {
    responses.set("/step/stp_1/artifacts", []);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      secondAction,
      firstAction,
      blockStart,
    ]);
    renderHero(trailingFailedRow);
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(secondAction.signed_url);
  });

  test("a trailing failed row with no action screenshots shows the block's only LLM screenshot", async () => {
    responses.set("/step/stp_1/artifacts", []);
    responses.set("/workflow_run_block/wrb_1/artifacts", [blockStart]);
    renderHero({ ...trailingFailedRow, actionOrder: 0 });
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(blockStart.signed_url);
  });

  test("a trailing failed row picks up the at-failure screenshot attached after the run ends, then stops polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    responses.set("/step/stp_1/artifacts", [secondAction, firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      secondAction,
      firstAction,
      blockStart,
    ]);
    const { setRunning } = renderHero(trailingFailedRow, undefined, true);
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(secondAction.signed_url);

    responses.set("/workflow_run_block/wrb_1/artifacts", [
      atFailure,
      secondAction,
      firstAction,
      blockStart,
    ]);
    setRunning(false);
    await waitFor(() =>
      expect(image.getAttribute("src")).toBe(atFailure.signed_url),
    );
    const blockFetches = () =>
      getSpy.mock.calls.filter(([url]) =>
        String(url).includes("/workflow_run_block/wrb_1/artifacts"),
      ).length;
    const fetchesAtFound = blockFetches();
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(blockFetches()).toBe(fetchesAtFound);
  });

  test("a finished run's trailing failed row with no at-failure screenshot stops polling the block", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    responses.set("/step/stp_1/artifacts", [secondAction, firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      secondAction,
      firstAction,
      blockStart,
    ]);
    renderHero(trailingFailedRow);
    const image = await screen.findByAltText("screenshot");
    expect(image.getAttribute("src")).toBe(secondAction.signed_url);

    await act(() => vi.advanceTimersByTimeAsync(120000));
    const blockFetches = getSpy.mock.calls.filter(([url]) =>
      String(url).includes("/workflow_run_block/wrb_1/artifacts"),
    ).length;
    expect(blockFetches).toBe(6);
  });

  test("a trailing failed row falls back when the at-failure image fails, and retries it once later", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mintMock.mockResolvedValue({ signed_url: atFailure.signed_url });
    responses.set("/step/stp_1/artifacts", [secondAction, firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      atFailure,
      secondAction,
      firstAction,
      blockStart,
    ]);
    renderHero(trailingFailedRow);
    const src = () => screen.getByAltText("screenshot").getAttribute("src");
    const image = await screen.findByAltText("screenshot");
    expect(src()).toBe(atFailure.signed_url);
    const sawUnavailable = watchForText("Screenshot unavailable.");

    fireEvent.error(image);
    await waitFor(() => expect(src()).toBe(secondAction.signed_url));

    await act(() => vi.advanceTimersByTimeAsync(5000));
    await waitFor(() => expect(src()).toBe(atFailure.signed_url));
    expect(sawUnavailable()).toBe(false);
  });

  test("switching to another block's failed row never shows the previous block's screenshot", async () => {
    let releaseSecondBlock: (artifacts: ArtifactApiResponse[]) => void = () =>
      undefined;
    const otherAtFailure = shot(
      "llm_at_failure_2",
      ArtifactType.LLMScreenshot,
      "2026-10-30T10:01:30Z",
    );
    const otherAction = shot(
      "action_other",
      ArtifactType.ActionScreenshot,
      "2026-10-30T10:00:05Z",
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(["step", "stp_2", "artifacts"], [otherAction]);
    responses.set("/step/stp_1/artifacts", [secondAction, firstAction]);
    responses.set("/workflow_run_block/wrb_1/artifacts", [
      atFailure,
      secondAction,
      firstAction,
    ]);
    responses.set(
      "/workflow_run_block/wrb_2/artifacts",
      new Promise<ArtifactApiResponse[]>((resolve) => {
        releaseSecondBlock = resolve;
      }),
    );
    const { setSelection } = renderHero(trailingFailedRow, client);
    await waitFor(() =>
      expect(screen.getByAltText("screenshot").getAttribute("src")).toBe(
        atFailure.signed_url,
      ),
    );

    setSelection({
      ...trailingFailedRow,
      stepId: "stp_2",
      actionOrder: 1,
      workflowRunBlockId: "wrb_2",
    });
    const src = () => screen.getByAltText("screenshot").getAttribute("src");
    expect(src()).toBe(otherAction.signed_url);

    releaseSecondBlock([otherAtFailure, otherAction]);
    await waitFor(() => expect(src()).toBe(otherAtFailure.signed_url));
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
