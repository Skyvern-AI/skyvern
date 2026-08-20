import type { HealEpisodeEngine, HealEpisodeStatus } from "../types/healTypes";

// Hue encodes the outcome; engine (harness vs floor) is encoded by emphasis
// (solid vs soft) so a fallback-engine recovery reads as lower-confidence
// without mis-coloring a verified success. Red is withheld on purpose — the run
// status owns red — so a failed heal is orange, never red.
type HealHue = "success" | "warning" | "orange" | "neutral";
type HealEmphasis = "solid" | "soft";

const healStatusLabels: Record<HealEpisodeStatus, string> = {
  fired_completed: "Self-healed",
  fired_unverified: "Recovered · unverified",
  fired_failed: "Heal failed",
  skipped: "No heal",
};

function healStatusLabel(status: HealEpisodeStatus): string {
  return healStatusLabels[status];
}

function healStatusHue(status: HealEpisodeStatus): HealHue {
  switch (status) {
    case "fired_completed":
      return "success";
    case "fired_unverified":
      return "warning";
    case "fired_failed":
      return "orange";
    case "skipped":
      return "neutral";
  }
}

// harness = the primary, instrumented engine (solid); floor = the weaker
// fallback that only runs when the harness can't (soft/outline).
function healEngineEmphasis(engine: HealEpisodeEngine): HealEmphasis {
  return engine === "harness" || engine === "code" ? "solid" : "soft";
}

// Engine names are internal jargon; surface the confidence relationship
// instead. Unknown engines de-emphasize to "fallback" (no silent confidence).
function healEngineLabel(engine: HealEpisodeEngine): string {
  if (engine === "harness" || engine === "code") return "primary";
  return "fallback";
}

const healSkipReasonLabels: Record<string, string> = {
  capped: "Attempt limit reached",
  adoption_failed: "Recovery not adopted",
  credential_unavailable: "Credential unavailable",
  timeout_class: "Timed out",
  insecure_code: "Unsafe code blocked",
  unclassifiable: "Unclassified",
};

function healSkipReasonLabel(reason: string | null): string {
  if (reason === null) return "-";
  return healSkipReasonLabels[reason] ?? reason.replace(/_/g, " ");
}

// Only claim "recovered" when something actually recovered; otherwise keep just
// the always-true "workflow version is unchanged" invariant. A "Recovered"
// message under a Heal failed / No heal state is the false confidence this UI
// exists to prevent.
function healPanelInvariant(recovered: boolean): string {
  return recovered
    ? "Recovered this run — your workflow version is unchanged."
    : "Your workflow version is unchanged.";
}

function healChipTooltip(healed: boolean): string {
  return healed
    ? "Runtime healing recovered this run. It never edits your workflow."
    : "Runtime healing attempted a recovery this run. It never edits your workflow.";
}

export {
  healChipTooltip,
  healEngineEmphasis,
  healEngineLabel,
  healPanelInvariant,
  healSkipReasonLabel,
  healStatusHue,
  healStatusLabel,
};
export type { HealEmphasis, HealHue };
