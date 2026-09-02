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

function track(completedKeys: readonly string[]): OnboardingTrackV1 {
  return {
    version: "onboarding_track_v1",
    state: "active",
    arm: "treatment",
    completed_count: completedKeys.length,
    total_count: 8,
    items: TRACK_KEYS.map((key) => ({
      key,
      completed_at: completedKeys.includes(key) ? "2026-09-01T12:00:00Z" : null,
      verification: SELF_ATTESTED_KEYS.has(key) ? "self" : "server",
    })),
  };
}
function renderTrack(
  value: OnboardingTrackV1,
  overrides: Partial<Parameters<typeof GettingStartedTrack>[0]> = {},
) {
  const props = {
    track: value,
    credentialUnlocked: true,
    intent: null,
    onAttest: vi.fn(),
    onDismiss: vi.fn(),
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
    .filter((link) => link.className.includes("bg-cta"))
    .map((link) => link.textContent);
afterEach(cleanup);

it("orders Keep going by intent and gives the first incomplete row the only primary action", () => {
  renderTrack(track(["first_scheduled_run"]), { intent: "extract_data" });
  expect(titlesIn("Keep going")).toEqual([
    "Current step: Run an agent from your own code",
    "Complete: Run your agent automatically",
    "Upcoming step: Use Skyvern from Claude, Cursor, or ChatGPT",
    "Upcoming step: Let your agent log in for you",
  ]);
  expect(primaryLinks()).toEqual(["Get an API key"]);
  expect(screen.queryByRole("region", { name: "Bring your team" })).toBeNull();
  expect(screen.queryByText("Bring a teammate")).toBeNull();
  expect(titlesIn("Stay in the loop")).toEqual([
    "Upcoming step: Star Skyvern on GitHub",
    "Upcoming step: Join the Discord",
    "Upcoming step: Follow for updates",
  ]);
  expect(screen.getByText("1 of 4 complete")).toBeTruthy();
  expect(
    screen
      .getAllByRole("link", { name: "How it works" })
      .map((link) => link.getAttribute("href")),
  ).toEqual([
    "https://www.skyvern.com/docs/developers/getting-started/quickstart",
    "https://www.skyvern.com/docs/developers/features/authentication-and-2fa",
  ]);
});

it("leads a nine-row track with the second-agent detail and omits it from eight-row tracks", () => {
  const base = track([]);
  renderTrack(
    {
      ...base,
      total_count: 9,
      items: [
        ...base.items,
        {
          key: SECOND_AGENT_KEY,
          completed_at: null,
          verification: "server",
        },
      ],
    },
    { rowDetails: { second_agent_run: <span>detail</span> } },
  );
  expect(titlesIn("Keep going")[0]).toBe("Current step: Run a second agent");
  expect(screen.getByText("detail")).toBeTruthy();
  expect(screen.getByText("0 of 5 complete")).toBeTruthy();
  expect(primaryLinks()).toEqual(["Run another workflow…"]);
  expect(
    screen
      .getByRole("link", { name: "Run another workflow…" })
      .getAttribute("href"),
  ).toBe("/agents");

  cleanup();
  renderTrack(base);
  expect(screen.queryByText("Run a second agent")).toBeNull();
});

it("locks the credential row on Free, keeps it first for fill_forms, and leaves it uncounted", () => {
  renderTrack(track([]), { credentialUnlocked: false, intent: "fill_forms" });
  expect(titlesIn("Keep going")[0]).toBe(
    "Locked: Let your agent log in for you",
  );
  expect(screen.getByText("Available on Hobby and up")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Upgrade" }).getAttribute("href"),
  ).toBe("/billing");
  expect(primaryLinks()).toEqual(["Open schedules"]);
  expect(screen.getByText("0 of 3 complete")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuemax")).toBe(
    "3",
  );
});

it("attests the clicked community row and never offers a client control on server rows", () => {
  const props = renderTrack(track([]));
  const keepGoing = screen.getByRole("region", { name: "Keep going" });
  expect(within(keepGoing).queryByRole("button")).toBeNull();
  fireEvent.click(screen.getAllByRole("button", { name: "Mark done" })[1]!);
  expect(props.onAttest).toHaveBeenCalledWith("discord_joined");
  fireEvent.click(screen.getByRole("button", { name: "Hide for now" }));
  expect(props.onDismiss).toHaveBeenCalledOnce();
  cleanup();
  const pending = renderTrack(track([]), { isPending: true });
  fireEvent.click(screen.getAllByRole("button", { name: "Mark done" })[0]!);
  fireEvent.click(screen.getByRole("button", { name: "Hide for now" }));
  expect(pending.onAttest).not.toHaveBeenCalled();
  expect(pending.onDismiss).not.toHaveBeenCalled();
});
