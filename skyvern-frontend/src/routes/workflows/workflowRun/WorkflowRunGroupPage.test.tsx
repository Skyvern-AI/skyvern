// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS } from "../hooks/useWorkflowRunQuery";
import {
  EXPORT_READ_TIMEOUT_MS,
  WorkflowRunGroupPage,
} from "./WorkflowRunGroupPage";

const { getMock, postMock, downloadMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  downloadMock: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ get: getMock, post: postMock })),
}));

vi.mock("@/util/downloadBlob", () => ({ downloadBlob: downloadMock }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const outcomes = [
  "completed",
  "failed",
  "unknown",
  "in_progress",
  "pending",
] as const;

const group = {
  workflow_run_group_id: "wrg_1",
  status: "active",
  items: outcomes.map((outcome, position) => ({
    key: `row_${position}`,
    position,
    workflow_run_id: `wr_${position}`,
    run_status:
      outcome === "pending"
        ? null
        : outcome === "in_progress"
          ? "running"
          : "completed",
    outcome,
  })),
};

function axiosError(status: number) {
  return new AxiosError(String(status), String(status), undefined, undefined, {
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
    data: {},
  });
}

function runFor(runId: string) {
  return {
    workflow_run_id: runId,
    status: runId === "wr_3" ? "running" : "completed",
    created_at: "2026-11-01T10:01:00.000000",
    finished_at: runId === "wr_3" ? null : "2026-11-01T10:05:00.000000",
    parameters: { company: `company ${runId}`, login: "cred_secret" },
    outputs: {
      lookup_output: { found: `found ${runId}` },
      summary_output: `newer version ${runId}`,
      extracted_information: [],
    },
    workflow: {
      modified_at: "2026-11-01T10:00:00.000000",
      workflow_definition: {
        parameters: [
          {
            key: "login",
            parameter_type: "workflow",
            workflow_parameter_type: "credential_id",
          },
          { key: "company", parameter_type: "workflow" },
        ],
        blocks: [{ output_parameter: { key: "lookup_output" } }],
      },
    },
  };
}

function respond(url: string) {
  if (url === "/workflow_run_groups/wrg_1") {
    return Promise.resolve({ data: group });
  }
  return Promise.resolve({ data: runFor(url.slice(url.lastIndexOf("/") + 1)) });
}

function renderPage(
  path = "/run-groups/wrg_1",
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/run-groups/:groupId"
            element={<WorkflowRunGroupPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("run group page", () => {
  it("shows each row's safe inputs, outcome, run link and pinned final output", async () => {
    getMock.mockImplementation(respond);
    renderPage();

    expect(await screen.findByText('{"found":"found wr_2"}')).toBeTruthy();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    expect(within(rows[0]!).getByText("company wr_0")).toBeTruthy();
    expect(within(rows[0]!).getByText("Completed")).toBeTruthy();
    expect(
      within(rows[0]!).getByRole("link", { name: "wr_0" }).getAttribute("href"),
    ).toBe("/runs/wr_0");
    expect(within(rows[1]!).getByText("Failed before acting")).toBeTruthy();
    expect(within(rows[2]!).getByText("Unknown, may have acted")).toBeTruthy();
    expect(await within(rows[3]!).findByText("company wr_3")).toBeTruthy();
    expect(within(rows[3]!).getByText("Pending")).toBeTruthy();
    expect(within(rows[4]!).getByText("Not started")).toBeTruthy();
    expect(screen.queryByText("cred_secret")).toBeNull();
    expect(screen.queryByText(/newer version/)).toBeNull();
    expect(getMock).not.toHaveBeenCalledWith(
      "/workflows/runs/wr_4",
      expect.anything(),
    );
    expect(screen.getByRole("list", { name: "Counts" }).textContent).toContain(
      "1 Failed before acting",
    );
  });

  it("stops through one group cancel and keeps the read view when it is denied", async () => {
    getMock.mockImplementation(respond);
    postMock.mockRejectedValue(axiosError(403));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop runs" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "You don't have permission to stop these runs.",
    );
    expect(postMock.mock.calls).toEqual([
      ["/workflow_run_groups/wrg_1/cancel"],
    ]);
    expect(screen.getAllByRole("row")).toHaveLength(6);
    expect(screen.getByText('{"found":"found wr_0"}')).toBeTruthy();
  });

  it("keeps the loaded rows when a group refresh fails and re-reads a run whose first read failed", async () => {
    let groupFails = false;
    let runFails = true;
    getMock.mockImplementation((url: string) => {
      if (url === "/workflows/runs/wr_1" && runFails) {
        return Promise.reject(axiosError(500));
      }
      if (url === "/workflow_run_groups/wrg_1" && groupFails) {
        return Promise.reject(axiosError(404));
      }
      return respond(url);
    });
    postMock.mockResolvedValue({ data: {} });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderPage("/run-groups/wrg_1?page=abc");

      await screen.findByText('{"found":"found wr_0"}');
      const failedRow = screen.getAllByRole("row")[2]!;
      expect(
        await within(failedRow).findAllByText("Couldn't load"),
      ).toHaveLength(2);

      runFails = false;
      await vi.advanceTimersByTimeAsync(RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS);
      expect(await within(failedRow).findByText("company wr_1")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
    expect(screen.getByText(/· 5 runs$/)).toBeTruthy();
    expect(
      [...screen.getByRole("list", { name: "Counts" }).children].map(
        (item) => item.textContent,
      ),
    ).toEqual([
      "1 Waiting",
      "1 Running",
      "1 Completed",
      "1 Failed before acting",
      "1 Unknown, may have acted",
    ]);

    groupFails = true;
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByText("Stop 2 unfinished runs?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop runs" }));

    expect(
      await screen.findByText(
        "Couldn't refresh this run group; showing the last loaded state.",
      ),
    ).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(6);
  });

  it("shows the last page for a page number past it", async () => {
    getMock.mockImplementation(respond);
    renderPage("/run-groups/wrg_1?page=99");

    expect(await screen.findByText('{"found":"found wr_0"}')).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(6);
  });

  it("lists an unreadable run as omitted and fails the export on any other read error", async () => {
    let failure = 500;
    getMock.mockImplementation((url: string) => {
      if (url === "/workflows/runs/wr_1") {
        return Promise.reject(axiosError(failure));
      }
      return respond(url);
    });
    renderPage();
    await screen.findByText('{"found":"found wr_0"}');

    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("no CSV was made");
    expect(downloadMock).not.toHaveBeenCalled();

    failure = 403;
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(downloadMock).toHaveBeenCalledTimes(1));
    const csv = (downloadMock.mock.calls[0]![0] as string[]).join("");
    const lines = csv.split("\r\n");
    expect(lines).toHaveLength(6);
    expect(lines[2]).toMatch(/^2,Failed before acting,.*,omitted,/);
    expect(lines[1]).toContain("company wr_0");
    expect(csv).not.toContain("cred_secret");
  });

  it("stops the unfinished rows through the group cancel and leaves finished rows as they were", async () => {
    let stopped = false;
    getMock.mockImplementation((url: string) =>
      url === "/workflow_run_groups/wrg_1" && stopped
        ? Promise.resolve({
            data: {
              ...group,
              status: "finished",
              items: group.items.map((item) =>
                item.outcome === "in_progress" || item.outcome === "pending"
                  ? { ...item, outcome: "canceled" }
                  : item,
              ),
            },
          })
        : respond(url),
    );
    postMock.mockImplementation(async () => {
      stopped = true;
      return { data: {} };
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop runs" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Stop" })).toBeNull(),
    );
    expect(postMock.mock.calls).toEqual([
      ["/workflow_run_groups/wrg_1/cancel"],
    ]);
    const rows = screen.getAllByRole("row").slice(1);
    expect(
      rows.map((row) => row.querySelectorAll("td")[2]!.textContent),
    ).toEqual([
      "Completed",
      "Failed before acting",
      "Unknown, may have acted",
      "Canceled",
      "Canceled",
    ]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("hides the output and inputs of a run whose workflow was edited in place after it finished", async () => {
    getMock.mockImplementation((url: string) => {
      if (url !== "/workflows/runs/wr_0") {
        return respond(url);
      }
      const edited = runFor("wr_0");
      edited.workflow.modified_at = "2026-11-01T10:06:00.000000";
      edited.workflow.workflow_definition.parameters =
        edited.workflow.workflow_definition.parameters.slice(0, 1);
      return Promise.resolve({ data: edited });
    });
    renderPage();

    const firstRow = (
      await screen.findByText(
        "Unavailable: the workflow was edited after this run",
      )
    ).closest("tr")!;
    expect(within(firstRow).queryByText("company wr_0")).toBeNull();
    expect(screen.queryByText('{"found":"found wr_0"}')).toBeNull();
    expect(screen.getByText('{"found":"found wr_2"}')).toBeTruthy();
  });

  it("aborts an export read that never answers and fails the export with a retry", async () => {
    let hang = false;
    let aborted = false;
    getMock.mockImplementation(
      (url: string, config?: { signal?: AbortSignal }) => {
        if (url === "/workflows/runs/wr_2" && hang) {
          return new Promise((_, reject) => {
            config?.signal?.addEventListener("abort", () => {
              aborted = true;
              reject(new Error("aborted"));
            });
          });
        }
        return respond(url);
      },
    );
    renderPage();
    await screen.findByText('{"found":"found wr_2"}');

    hang = true;
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
      await vi.advanceTimersByTimeAsync(EXPORT_READ_TIMEOUT_MS);
    } finally {
      vi.useRealTimers();
    }

    const alert = await screen.findByRole("alert");
    expect(aborted).toBe(true);
    expect(alert.textContent).toContain("no CSV was made");
    expect(downloadMock).not.toHaveBeenCalled();

    hang = false;
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(downloadMock).toHaveBeenCalledTimes(1));
    const lines = (downloadMock.mock.calls[0]![0] as string[])
      .join("")
      .split("\r\n");
    expect(lines[3]).toContain("/runs/wr_2,");
    expect(lines[3]).not.toContain("unavailable");
  });

  it("stops every other export read at the first read error that is not an omitted run", async () => {
    let failExport = false;
    const aborted: string[] = [];
    getMock.mockImplementation(
      (url: string, config?: { signal?: AbortSignal }) => {
        if (!failExport || url === "/workflow_run_groups/wrg_1") {
          return respond(url);
        }
        if (url === "/workflows/runs/wr_0") {
          return Promise.reject(axiosError(500));
        }
        return new Promise((_, reject) => {
          config?.signal?.addEventListener("abort", () => {
            aborted.push(url);
            reject(new Error("aborted"));
          });
        });
      },
    );
    renderPage();
    await screen.findByText('{"found":"found wr_0"}');

    failExport = true;
    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "no CSV was made",
    );
    expect(aborted.sort()).toEqual([
      "/workflows/runs/wr_1",
      "/workflows/runs/wr_2",
      "/workflows/runs/wr_3",
    ]);
    expect(downloadMock).not.toHaveBeenCalled();
  });

  it("reads a cached finished run again when the page mounts", async () => {
    let found = "first read";
    getMock.mockImplementation((url: string) => {
      if (url !== "/workflows/runs/wr_0") {
        return respond(url);
      }
      const run = runFor("wr_0");
      run.outputs.lookup_output = { found };
      return Promise.resolve({ data: run });
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5 * 60 * 1000 } },
    });
    renderPage("/run-groups/wrg_1", client);
    await screen.findByText('{"found":"first read"}');
    cleanup();

    found = "second read";
    renderPage("/run-groups/wrg_1", client);

    expect(await screen.findByText('{"found":"second read"}')).toBeTruthy();
  });

  it("treats a row whose current read is denied as unreadable even with an earlier read cached", async () => {
    let denied = false;
    getMock.mockImplementation((url: string) =>
      url === "/workflows/runs/wr_0" && denied
        ? Promise.reject(axiosError(403))
        : respond(url),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    renderPage("/run-groups/wrg_1", client);
    await screen.findByText('{"found":"found wr_0"}');

    denied = true;
    await client.refetchQueries({ queryKey: ["workflowRun", "wr_0"] });
    const firstRow = screen.getAllByRole("row")[1]!;
    expect(await within(firstRow).findAllByText("Not readable")).toHaveLength(
      2,
    );
    expect(within(firstRow).queryByText("company wr_0")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(downloadMock).toHaveBeenCalledTimes(1));
    const csv = (downloadMock.mock.calls[0]![0] as string[]).join("");
    expect(csv.split("\r\n")[1]).toMatch(/^1,Completed,.*,omitted,/);
    expect(csv).not.toContain("company wr_0");
  });
});
