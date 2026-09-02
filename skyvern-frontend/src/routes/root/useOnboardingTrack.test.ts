import { expect, it } from "vitest";
import {
  countedTrackItems,
  parseOnboardingTrack,
  TRACK_KEYS,
} from "./useOnboardingTrack";

const validTrack = () => ({
  version: "onboarding_track_v1",
  state: "active",
  arm: "treatment",
  completed_count: 1,
  total_count: 8,
  items: TRACK_KEYS.map((key, index) => ({
    key,
    completed_at: index === 0 ? "2026-09-01T00:00:00Z" : null,
    verification:
      key === "github_starred" ||
      key === "discord_joined" ||
      key === "social_followed"
        ? "self"
        : "server",
  })),
});

it("accepts a well-formed track", () => {
  expect(parseOnboardingTrack(validTrack())?.completed_count).toBe(1);
});

it("rejects the retired six-key shape", () => {
  const track = validTrack();
  expect(
    parseOnboardingTrack({
      ...track,
      total_count: 6,
      items: track.items.filter(
        (item) => item.key !== "mcp_installed" && item.key !== "github_starred",
      ),
    }),
  ).toBeNull();
});

it("rejects a count that disagrees with the rows", () => {
  expect(
    parseOnboardingTrack({ ...validTrack(), completed_count: 2 }),
  ).toBeNull();
});

it("rejects rows out of the fixed order", () => {
  const track = validTrack();
  [track.items[0], track.items[1]] = [track.items[1]!, track.items[0]!];
  expect(parseOnboardingTrack(track)).toBeNull();
});

it("rejects a server row that claims self attestation", () => {
  const track = validTrack();
  track.items[0]!.verification = "self";
  expect(parseOnboardingTrack(track)).toBeNull();
});

it("rejects an impossible completion date", () => {
  const track = validTrack();
  track.items[0]!.completed_at = "2026-02-30T00:00:00Z";
  expect(parseOnboardingTrack(track)).toBeNull();
});

it("counts only production rows and drops a locked credential row", () => {
  const track = parseOnboardingTrack(validTrack())!;
  expect(countedTrackItems(track, true).map((row) => row.key)).toEqual([
    "first_scheduled_run",
    "first_api_run",
    "mcp_installed",
    "credential_saved",
  ]);
  expect(countedTrackItems(track, false).map((row) => row.key)).toEqual([
    "first_scheduled_run",
    "first_api_run",
    "mcp_installed",
  ]);
});

it("rejects a date-only completion timestamp", () => {
  const track = validTrack();
  track.items[0]!.completed_at = "2026-09-01";
  expect(parseOnboardingTrack(track)).toBeNull();
});
