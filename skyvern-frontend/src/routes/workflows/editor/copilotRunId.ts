/**
 * The run id the docked Copilot should be grounded in. Only the studio (embedded)
 * mount focuses a run via ?wr=; off-studio mounts must fall through to the route
 * param, so the studio run id is suppressed there.
 */
export function copilotRunId({
  embedded,
  studioRunId,
}: {
  embedded: boolean;
  studioRunId?: string;
}): string | undefined {
  return embedded ? studioRunId : undefined;
}
