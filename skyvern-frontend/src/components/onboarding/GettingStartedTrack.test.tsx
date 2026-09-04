import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import {
  SECOND_AGENT_KEY,
  SELF_ATTESTED_KEYS,
  TRACK_KEYS,
  type OnboardingTrackV1,
} from "@/routes/root/useOnboardingTrack";
import { GettingStartedTrack } from "./GettingStartedTrack";

const completedAt = "2026-09-01T12:00:00Z";
const noProgress = {
  first_agent_created: null,
  first_successful_run: null,
};
const completedProgress = {
  first_agent_created: completedAt,
  first_successful_run: completedAt,
};

function track(completedKeys: readonly string[]): OnboardingTrackV1 {
  return {
    version: "onboarding_track_v1",
    state: "active",
    arm: "treatment",
    completed_count: completedKeys.length,
    total_count: 8,
    items: TRACK_KEYS.map((key) => ({
      key,
      completed_at: completedKeys.includes(key) ? completedAt : null,
      verification: SELF_ATTESTED_KEYS.has(key) ? "self" : "server",
    })),
  };
}

function nineItemTrack(
  completedKeys: readonly string[] = [],
): OnboardingTrackV1 {
  const base = track(completedKeys);
  return {
    ...base,
    total_count: 9,
    items: [
      ...base.items,
      {
        key: SECOND_AGENT_KEY,
        completed_at: completedKeys.includes(SECOND_AGENT_KEY)
          ? completedAt
          : null,
        verification: "server",
      },
    ],
  };
}

function renderTrack(
  value: OnboardingTrackV1,
  overrides: Partial<Parameters<typeof GettingStartedTrack>[0]> = {},
) {
  const props = {
    track: value,
    progress: noProgress,
    credentialUnlocked: true,
    intent: null,
    onAttest: vi.fn(),
    onRestore: vi.fn(),
    isPending: false,
    ...overrides,
  };
  render(
    <MemoryRouter>
      <GettingStartedTrack {...props} />
    </MemoryRouter>,
  );
  return props;
}

const titlesIn = (group: string) =>
  within(screen.getByRole("region", { name: group }))
    .getAllByRole("listitem")
    .map((row) => row.querySelector("p")?.textContent);
const primaryLinks = () =>
  screen
    .getAllByRole("link")
    .filter((link) => link.className.includes("bg-cta"));
const expectProgress = (done: number, total: number) => {
  expect(screen.getByText(`${done} of ${total} done`)).toBeTruthy();
  const progressbar = screen.getByRole("progressbar", {
    name: "Getting started progress",
  });
  expect(progressbar.getAttribute("aria-valuenow")).toBe(String(done));
  expect(progressbar.getAttribute("aria-valuemax")).toBe(String(total));
  expect(progressbar.children).toHaveLength(total);
};

afterEach(cleanup);

it.each([
  ["no progress data", null, "Open schedules"],
  ["a fresh account", noProgress, "Describe your first agent"],
  [
    "only the first agent created",
    { first_agent_created: completedAt, first_successful_run: null },
    "Run agent",
  ],
  ["the first agent complete", completedProgress, "Open schedules"],
])("renders one filled action with %s", (_, progress, actionName) => {
  renderTrack(track([]), { progress });
  expect(primaryLinks()).toHaveLength(1);
  expect(primaryLinks()[0]).toBe(
    screen.getByRole("link", { name: actionName }),
  );
});

it("renders the three first-agent steps with their shipped destinations", () => {
  renderTrack(track([]));
  const rows = within(
    screen.getByRole("region", { name: "First agent" }),
  ).getAllByRole("listitem");
  const describeLink = screen.getByRole("link", {
    name: "Describe your first agent",
  });
  const runLink = screen.getByRole("link", { name: "Run agent" });

  expect(rows).toHaveLength(3);
  expect(
    rows.map((row) => row.querySelector("p .sr-only")?.textContent),
  ).toEqual(["Complete: ", "Current step: ", "Upcoming step: "]);
  expect(rows[0]?.querySelector("a")).toBeNull();
  expect(rows[1]?.contains(describeLink)).toBe(true);
  expect(describeLink.getAttribute("href")).toBe("/discover?focus=prompt");
  expect(rows[2]?.contains(runLink)).toBe(true);
  expect(runLink.getAttribute("href")).toBe("/agents");
  expect(
    within(screen.getByRole("region", { name: "First agent" }))
      .getAllByRole("link", {
        name: /How it works.*opens in new tab/,
      })
      .map((link) => link.getAttribute("href")),
  ).toEqual([
    "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
    "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  ]);
});

it("renders no filled action after all required rows are complete", () => {
  renderTrack(
    track([
      "first_scheduled_run",
      "first_api_run",
      "mcp_installed",
      "credential_saved",
    ]),
    { progress: completedProgress },
  );
  expect(primaryLinks()).toHaveLength(0);
  expect(
    screen.getByRole("button", { name: "Mark Star Skyvern on GitHub done" }),
  ).toBeTruthy();
});

it.each([
  ["fresh paid", track([]), true, noProgress, 1, 7],
  ["fresh Free", track([]), false, noProgress, 1, 6],
  ["nine-item", nineItemTrack(), true, noProgress, 1, 8],
  ["first agent done", track([]), true, completedProgress, 3, 7],
])(
  "counts progress and renders one bar segment per step for %s",
  (_, value, credentialUnlocked, progress, done, total) => {
    renderTrack(value, { credentialUnlocked, progress });
    expectProgress(done, total);
  },
);

it("collapses a completed first-agent group and expands its three rows", () => {
  renderTrack(track([]), { progress: completedProgress });
  expect(screen.getByText("First agent ready")).toBeTruthy();
  expect(screen.queryByText("Create your first agent")).toBeNull();
  const toggle = screen.getByRole("button", {
    name: "Show first agent steps",
  });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(
    document.getElementById(toggle.getAttribute("aria-controls")!),
  ).toBeTruthy();

  fireEvent.click(toggle);

  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("button", { name: "Hide first agent steps" })).toBe(
    toggle,
  );
  expect(titlesIn("First agent")).toHaveLength(3);
  expect(screen.getByText("Create your first agent")).toBeTruthy();
  expect(screen.getByText("Run your first agent")).toBeTruthy();
});

it("labels required group completion and community work as optional", () => {
  renderTrack(
    track([
      "first_scheduled_run",
      "first_api_run",
      "mcp_installed",
      "credential_saved",
    ]),
    { progress: completedProgress },
  );
  expect(
    within(screen.getByRole("region", { name: "First agent" })).getByText(
      "Done",
    ),
  ).toBeTruthy();
  expect(
    within(screen.getByRole("region", { name: "Keep going" })).getByText(
      "Done",
      { selector: "span" },
    ),
  ).toBeTruthy();
  expect(
    within(screen.getByRole("region", { name: "Stay in the loop" })).getByText(
      "Optional",
    ),
  ).toBeTruthy();
});

it("orders Keep going by intent after first-agent work is complete", () => {
  renderTrack(track(["first_scheduled_run"]), {
    progress: completedProgress,
    intent: "extract_data",
  });
  expect(titlesIn("Keep going")).toEqual([
    "Current step: Run an agent from your own code",
    "Complete: Run your agent automatically",
    "Upcoming step: Use Skyvern from Claude, Cursor, or ChatGPT",
    "Upcoming step: Let your agent log in for you",
  ]);
  expect(primaryLinks()[0]).toBe(
    screen.getByRole("link", { name: "Get an API key" }),
  );
  expect(screen.queryByRole("region", { name: "Bring your team" })).toBeNull();
  expect(screen.queryByText("Bring a teammate")).toBeNull();
  expect(titlesIn("Stay in the loop")).toEqual([
    "Upcoming step: Star Skyvern on GitHub",
    "Upcoming step: Join the Discord",
    "Upcoming step: Follow for updates",
  ]);
  expect(
    within(screen.getByRole("region", { name: "Keep going" }))
      .getAllByRole("link", {
        name: /How it works.*opens in new tab/,
      })
      .map((link) => link.getAttribute("href")),
  ).toEqual([
    "https://www.skyvern.com/docs/developers/getting-started/quickstart",
    "https://www.skyvern.com/docs/developers/features/authentication-and-2fa",
  ]);
});

it("uses agent copy and leads a nine-item Keep going group with the second-agent detail", () => {
  renderTrack(nineItemTrack(), {
    progress: completedProgress,
    rowDetails: { second_agent_run: <span>detail</span> },
  });
  const row = screen.getByText("Run a second agent").closest("li");
  expect(row).not.toBeNull();
  expect(row?.textContent?.toLowerCase()).toContain("agent");
  expect(row?.textContent?.toLowerCase()).not.toContain("workflow");
  expect(screen.getByText("detail")).toBeTruthy();
  expect(primaryLinks()).toHaveLength(1);
  expect(primaryLinks()[0]).toBe(
    screen.getByRole("link", { name: "Run another agent" }),
  );
  expect(primaryLinks()[0]?.getAttribute("href")).toBe("/agents");

  cleanup();
  renderTrack(track([]));
  expect(screen.queryByText("Run a second agent")).toBeNull();
});

it("moves a locked Free credential row to the end without counting it", () => {
  renderTrack(track([]), {
    progress: completedProgress,
    credentialUnlocked: false,
    intent: "fill_forms",
  });
  const rows = within(
    screen.getByRole("region", { name: "Keep going" }),
  ).getAllByRole("listitem");
  const lastRow = rows[rows.length - 1]!;
  expect(lastRow.textContent).toContain("Let your agent log in for you");
  expect(lastRow.textContent).toContain("Available on Hobby and up");
  expect(
    within(lastRow).getByRole("link", { name: "Upgrade" }).getAttribute("href"),
  ).toBe("/billing");
  expect(primaryLinks()).toHaveLength(1);
  expect(primaryLinks()[0]).toBe(
    screen.getByRole("link", { name: "Open schedules" }),
  );
  expectProgress(3, 6);
});

it("does not label a Free group done while its locked row remains", () => {
  renderTrack(
    track(["first_scheduled_run", "first_api_run", "mcp_installed"]),
    { progress: completedProgress, credentialUnlocked: false },
  );
  expect(
    within(screen.getByRole("region", { name: "Keep going" })).getByText(
      "3 steps",
    ),
  ).toBeTruthy();
});

it("uses the community ring as the attest control and keeps links enabled while pending", () => {
  const props = renderTrack(track([]));
  const ring = screen.getByRole("button", {
    name: "Mark Join the Discord done",
  });
  fireEvent.click(ring);
  expect(props.onAttest).toHaveBeenCalledWith("discord_joined");
  expect(screen.queryByText("Mark done")).toBeNull();
  expect(screen.queryByText("Hide for now")).toBeNull();

  cleanup();
  const pending = renderTrack(track([]), { isPending: true });
  const pendingRing = screen.getByRole("button", {
    name: "Mark Star Skyvern on GitHub done",
  });
  expect(pendingRing.getAttribute("aria-disabled")).toBe("true");
  expect(pendingRing.getAttribute("aria-busy")).toBe("true");
  fireEvent.click(pendingRing);
  expect(pending.onAttest).not.toHaveBeenCalled();
  expect(
    screen
      .getAllByRole("button")
      .filter((button) => button.hasAttribute("aria-disabled"))
      .every((button) =>
        button.getAttribute("aria-label")?.startsWith("Mark "),
      ),
  ).toBe(true);
  for (const link of screen.getAllByRole("link")) {
    expect(link.hasAttribute("aria-disabled")).toBe(false);
    expect(link.className).not.toContain("pointer-events-none");
    expect(link.className).not.toContain("opacity-");
  }
});

it("replaces a completed community ring with the done badge", () => {
  renderTrack(track(["github_starred"]));
  const completedRow = screen.getByText("Star Skyvern on GitHub").closest("li");
  expect(completedRow).not.toBeNull();
  expect(
    within(completedRow!).queryByRole("button", { name: /Mark .* done/ }),
  ).toBeNull();
  expect(completedRow!.querySelector('[aria-hidden="true"] svg')).toBeTruthy();

  const incompleteRow = screen.getByText("Join the Discord").closest("li");
  expect(incompleteRow).not.toBeNull();
  expect(
    within(incompleteRow!).getByRole("button", {
      name: "Mark Join the Discord done",
    }),
  ).toBeTruthy();
  expect(
    within(incompleteRow!).getByRole("link", {
      name: /Join Discord.*opens in new tab/,
    }),
  ).toBeTruthy();
});

it("keeps the progress live region mounted while idle", () => {
  renderTrack(track([]));
  const liveRegion = document.querySelector('[aria-live="polite"]');
  expect(liveRegion).not.toBeNull();
  expect(liveRegion?.textContent).toBe("");
});

it("keeps only Resume in the dismissed state", () => {
  const value = { ...track([]), state: "dismissed" as const };
  const props = renderTrack(value);
  const resume = screen.getByRole("button", { name: "Resume" });
  expect(screen.getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByRole("list")).toBeNull();
  fireEvent.click(resume);
  expect(props.onRestore).toHaveBeenCalledOnce();
});
