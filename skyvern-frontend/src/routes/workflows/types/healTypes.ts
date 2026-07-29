export type HealEpisodeStatus =
  | "fired_completed"
  | "fired_unverified"
  | "fired_failed"
  | "skipped";

export type HealEpisodeEngine = "harness" | "floor" | "code";

export type HealOutputObligation = "none" | "observed" | "vestigial" | null;

export type HealEpisodeView = {
  heal_episode_id: string;
  workflow_permanent_id: string;
  workflow_id: string;
  workflow_run_id: string;
  workflow_run_block_id: string;
  block_label: string | null;
  engine: HealEpisodeEngine;
  status: HealEpisodeStatus;
  skip_reason: string | null;
  snapshot_available: boolean;
  convergence_eligible: boolean;
  parameter_binding_keys: string[];
  exception_class: string | null;
  failing_line: number | null;
  matched_step_index: number | null;
  escalation_task_id: string | null;
  wall_clock_ms: number | null;
  action_count: number | null;
  output_obligation: HealOutputObligation;
  dom_snapshot_artifact_id: string | null;
  scout_transcript_artifact_id: string | null;
  screenshot_artifact_id: string | null;
  created_at: string;
  modified_at: string;
};

export type RunHealSummary = {
  blocks_healed: number;
  blocks_outcome_risk: string[];
  blocks_with_heal_attempt: number;
};

export type RunHealEpisodesResponse = {
  episodes: HealEpisodeView[];
  summary: RunHealSummary;
};

export type RunsHealSummaryBatchResponse = {
  summaries: Record<string, RunHealSummary>;
};
