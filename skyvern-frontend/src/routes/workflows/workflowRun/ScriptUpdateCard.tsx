import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from "@/components/ui/accordion";
import { CodeIcon } from "@radix-ui/react-icons";
import type { ScriptFallbackEpisode } from "@/routes/workflows/types/scriptTypes";
import { useScriptVersionsQuery } from "@/routes/workflows/hooks/useScriptVersionsQuery";
import { useScriptVersionCodeQuery } from "@/routes/workflows/hooks/useScriptVersionCodeQuery";
import { ScriptDiffViewer } from "./ScriptDiffViewer";

const ERROR_TRUNCATE_LENGTH = 150;

const fallbackTypeLabels: Record<
  ScriptFallbackEpisode["fallback_type"],
  string
> = {
  element: "Element fallback",
  full_block: "Full block fallback",
  conditional_agent: "Conditional agent",
};

function ScriptUpdateCard({
  episodes,
  scriptId,
}: {
  episodes: ScriptFallbackEpisode[];
  scriptId: string | null | undefined;
}) {
  const episodesWithUpdates = episodes.filter(
    (ep) => ep.new_script_revision_id,
  );

  if (episodesWithUpdates.length === 0) {
    return null;
  }

  // Deduplicate: multiple episodes can point to the same new revision.
  // Group by new_script_revision_id and show one diff per revision.
  const revisionIds = [
    ...new Set(
      episodesWithUpdates
        .map((ep) => ep.new_script_revision_id)
        .filter(Boolean),
    ),
  ] as string[];

  // We also need the "before" revision — the script_revision_id from the
  // first episode that triggered this update.
  const beforeRevisionId = episodesWithUpdates[0]?.script_revision_id ?? null;

  return (
    <div className="rounded-md border border-blue-600/50 bg-blue-950/30 p-4">
      <Accordion type="single" collapsible>
        <AccordionItem value="script-update" className="border-none">
          <AccordionTrigger className="py-0 hover:no-underline">
            <div className="flex items-center gap-2">
              <CodeIcon className="size-4 text-blue-400" />
              <span className="font-medium text-blue-300">
                Script updated after this run
              </span>
              <Badge className="bg-blue-800/60 text-blue-200 hover:bg-blue-800/60">
                {episodesWithUpdates.length}{" "}
                {episodesWithUpdates.length === 1 ? "update" : "updates"}
              </Badge>
            </div>
          </AccordionTrigger>
          <AccordionContent className="pb-0 pt-4">
            <div className="space-y-3">
              {episodesWithUpdates.map((episode) => (
                <EpisodeDetail key={episode.episode_id} episode={episode} />
              ))}
              {scriptId && revisionIds.length > 0 && beforeRevisionId && (
                <DiffSection
                  scriptId={scriptId}
                  beforeRevisionId={beforeRevisionId}
                  afterRevisionId={revisionIds[revisionIds.length - 1]!}
                />
              )}
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

function EpisodeDetail({ episode }: { episode: ScriptFallbackEpisode }) {
  const [expanded, setExpanded] = useState(false);
  const errorMessage = episode.error_message ?? "";
  const isTruncated = errorMessage.length > ERROR_TRUNCATE_LENGTH;
  const displayMessage =
    isTruncated && !expanded
      ? errorMessage.slice(0, ERROR_TRUNCATE_LENGTH) + "…"
      : errorMessage;

  return (
    <div className="rounded border border-slate-700 bg-slate-800/50 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-slate-200">{episode.block_label}</span>
        <Badge variant="secondary" className="text-xs">
          {fallbackTypeLabels[episode.fallback_type] ?? episode.fallback_type}
        </Badge>
        {episode.fallback_succeeded === true && (
          <Badge variant="success" className="text-xs">
            Recovered
          </Badge>
        )}
        {episode.fallback_succeeded === false && (
          <Badge variant="destructive" className="text-xs">
            Failed
          </Badge>
        )}
        {episode.fallback_succeeded === null && (
          <Badge variant="secondary" className="text-xs opacity-60">
            Pending review
          </Badge>
        )}
      </div>
      {errorMessage && (
        <div className="mt-2">
          <p className="text-xs text-slate-400">{displayMessage}</p>
          {isTruncated && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="mt-1 text-xs text-blue-400 hover:text-blue-300"
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function DiffSection({
  scriptId,
  beforeRevisionId,
  afterRevisionId,
}: {
  scriptId: string;
  beforeRevisionId: string;
  afterRevisionId: string;
}) {
  const [showDiff, setShowDiff] = useState(false);

  const { data: versions, isFetched: versionsFetched } = useScriptVersionsQuery(
    { scriptId },
  );

  // Map revision IDs to version numbers
  const beforeVersion =
    versions?.versions.find((v) => v.script_revision_id === beforeRevisionId)
      ?.version ?? null;
  const afterVersion =
    versions?.versions.find((v) => v.script_revision_id === afterRevisionId)
      ?.version ?? null;

  const { data: beforeCode, isFetched: beforeFetched } =
    useScriptVersionCodeQuery({
      scriptId: showDiff ? scriptId : null,
      version: showDiff ? beforeVersion : null,
    });
  const { data: afterCode, isFetched: afterFetched } =
    useScriptVersionCodeQuery({
      scriptId: showDiff ? scriptId : null,
      version: showDiff ? afterVersion : null,
    });

  const beforeText = beforeCode?.main_script ?? "";
  const afterText = afterCode?.main_script ?? "";
  const versionsReady = beforeVersion != null && afterVersion != null;
  const isLoading =
    showDiff &&
    (!versionsFetched || (versionsReady && (!beforeFetched || !afterFetched)));
  const hasBothVersions = beforeText !== "" && afterText !== "";
  // Only show "not available" after the versions query has settled — otherwise
  // the message would flash while the version list is still loading.
  const noCodeAvailable =
    showDiff &&
    versionsFetched &&
    (!versionsReady || (beforeFetched && afterFetched && !hasBothVersions));

  return (
    <div className="pt-1">
      <button
        onClick={() => setShowDiff(!showDiff)}
        className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 hover:underline"
      >
        <CodeIcon className="size-3.5" />
        {showDiff ? "Hide code changes" : "View code changes"}
        {beforeVersion != null && afterVersion != null && (
          <span className="text-xs text-slate-500">
            v{beforeVersion} → v{afterVersion}
          </span>
        )}
      </button>
      {showDiff && isLoading && (
        <p className="mt-2 text-xs text-slate-500">Loading diff…</p>
      )}
      {showDiff && hasBothVersions && (
        <div className="mt-3 overflow-hidden rounded border border-slate-700">
          <ScriptDiffViewer original={beforeText} modified={afterText} />
        </div>
      )}
      {showDiff && noCodeAvailable && (
        <p className="mt-2 text-xs text-slate-500">
          Code not available for these versions.
        </p>
      )}
    </div>
  );
}

export { ScriptUpdateCard };
