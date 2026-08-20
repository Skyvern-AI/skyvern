import { useMemo } from "react";
import {
  CheckCircledIcon,
  ExclamationTriangleIcon,
  MinusCircledIcon,
  QuestionMarkCircledIcon,
} from "@radix-ui/react-icons";

import { useRunHealEpisodesQuery } from "../hooks/useRunHealEpisodesQuery";
import type { HealEpisodeStatus, HealEpisodeView } from "../types/healTypes";
import {
  healEngineEmphasis,
  healEngineLabel,
  healPanelInvariant,
  healSkipReasonLabel,
  healStatusHue,
  healStatusLabel,
  type HealEmphasis,
  type HealHue,
} from "./healStatus";

type Props = {
  workflowRunId?: string;
  workflowRunBlockId: string;
};

// hue = outcome, emphasis = engine (solid harness / soft-outline fallback).
const hueClassName: Record<HealHue, Record<HealEmphasis, string>> = {
  success: {
    solid: "bg-success/15 text-success",
    soft: "border border-success/40 text-success",
  },
  warning: {
    solid: "bg-warning/15 text-warning",
    soft: "border border-warning/40 text-warning",
  },
  orange: {
    solid: "bg-orange-500/15 text-orange-600 dark:text-orange-400",
    soft: "border border-orange-500/40 text-orange-600 dark:text-orange-400",
  },
  neutral: {
    solid: "bg-muted text-muted-foreground",
    soft: "border border-border text-muted-foreground",
  },
};

const statusIcon: Record<HealEpisodeStatus, typeof CheckCircledIcon> = {
  fired_completed: CheckCircledIcon,
  fired_unverified: QuestionMarkCircledIcon,
  fired_failed: ExclamationTriangleIcon,
  skipped: MinusCircledIcon,
};

function formatDuration(wallClockMs: number | null): string {
  if (wallClockMs === null) {
    return "-";
  }
  return `${wallClockMs.toLocaleString()} ms`;
}

function formatActionCount(actionCount: number | null): string {
  if (actionCount === null) {
    return "-";
  }
  return `${actionCount.toLocaleString()} action${actionCount === 1 ? "" : "s"}`;
}

function StatusBadge({ episode }: { episode: HealEpisodeView }) {
  const hue = healStatusHue(episode.status);
  const emphasis = healEngineEmphasis(episode.engine);
  const Icon = statusIcon[episode.status];
  const label = healStatusLabel(episode.status);

  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${hueClassName[hue][emphasis]}`}
    >
      <Icon className="h-3 w-3" />
      {label}
    </span>
  );
}

function EvidenceChips({ episode }: { episode: HealEpisodeView }) {
  const chips: Array<string> = [];

  if (episode.dom_snapshot_artifact_id) {
    chips.push("DOM snapshot");
  }
  if (episode.scout_transcript_artifact_id) {
    chips.push("scout transcript");
  }
  if (episode.screenshot_artifact_id) {
    chips.push("screenshot");
  }

  if (chips.length === 0) {
    return null;
  }

  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {chips.map((chip) => (
        <span
          key={`${episode.heal_episode_id}-${chip}`}
          className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground"
        >
          {chip}
        </span>
      ))}
    </div>
  );
}

function BlockHealPanel({ workflowRunId, workflowRunBlockId }: Props) {
  const { data } = useRunHealEpisodesQuery({ workflowRunId });

  const blockEpisodes = useMemo(
    () =>
      (data?.episodes ?? []).filter(
        (episode) => episode.workflow_run_block_id === workflowRunBlockId,
      ),
    [data?.episodes, workflowRunBlockId],
  );

  if (blockEpisodes.length === 0) {
    return null;
  }

  const blockRecovered = blockEpisodes.some(
    (episode) =>
      episode.status === "fired_completed" ||
      episode.status === "fired_unverified",
  );

  return (
    <div className="border-b border-border bg-slate-elevation1 px-3 py-3">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground dark:text-slate-500">
        Runtime healing
      </div>
      <div className="space-y-2">
        {blockEpisodes.map((episode) => (
          <div
            key={episode.heal_episode_id}
            className="rounded border border-border bg-slate-elevation2 p-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge episode={episode} />
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted-foreground dark:bg-slate-700">
                {healEngineLabel(episode.engine)}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {formatDuration(episode.wall_clock_ms)}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {formatActionCount(episode.action_count)}
              </span>
            </div>
            {episode.status === "skipped" && (
              <div className="mt-1 text-[11px] text-muted-foreground">
                Skip reason: {healSkipReasonLabel(episode.skip_reason)}
              </div>
            )}
            <EvidenceChips episode={episode} />
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        {healPanelInvariant(blockRecovered)}
      </p>
    </div>
  );
}

export { BlockHealPanel };
