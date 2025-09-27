import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { basicLocalTimeFormat } from "@/util/timeFormat";
import {
  useWorkflowVersionsQuery,
  WorkflowVersion,
} from "../../hooks/useWorkflowVersionsQuery";

type Props = {
  workflowPermanentId: string;
  onCompare?: (
    version1: WorkflowVersion,
    version2: WorkflowVersion,
    mode?: "visual" | "json",
  ) => void;
};

function WorkflowHistoryPanel({ workflowPermanentId, onCompare }: Props) {
  const { data: versions, isLoading } = useWorkflowVersionsQuery({
    workflowPermanentId,
  });
  const [selectedVersions, setSelectedVersions] = useState<Set<number>>(
    new Set(),
  );

  // Set default selection: current (latest) and previous version
  useEffect(() => {
    if (versions && versions.length > 0) {
      // Versions are already sorted by version descending from the backend
      const defaultSelection = new Set<number>();

      // Select the latest version (current)
      const firstVersion = versions[0];
      if (firstVersion) defaultSelection.add(firstVersion.version);

      // Select the previous version if it exists
      const secondVersion = versions[1];
      if (secondVersion) defaultSelection.add(secondVersion.version);

      setSelectedVersions(defaultSelection);
    }
  }, [versions]);

  const handleVersionToggle = (version: number) => {
    const newSelected = new Set(selectedVersions);

    if (newSelected.has(version)) {
      newSelected.delete(version);
    } else {
      // If already at max 2 selections, remove the oldest selection
      if (newSelected.size >= 2) {
        const versionsArray = Array.from(newSelected);
        // Remove first (oldest) selection
        const versionToDelete = versionsArray[0];
        if (versionToDelete) newSelected.delete(versionToDelete);
      }
      newSelected.add(version);
    }

    setSelectedVersions(newSelected);
  };

  const handleCompare = (mode: "visual" | "json" = "visual") => {
    if (selectedVersions.size === 2 && versions) {
      const selectedVersionsArray = Array.from(selectedVersions);
      const version1 = versions.find(
        (v) => v.version === selectedVersionsArray[0],
      );
      const version2 = versions.find(
        (v) => v.version === selectedVersionsArray[1],
      );

      if (version1 && version2) {
        onCompare?.(version1, version2, mode);
      }
    }
  };

  // Versions are already sorted by the backend, no need to sort again
  const sortedVersions = versions || [];
  const canCompare = selectedVersions.size === 2;

  return (
    <div className="flex h-full w-[25rem] flex-col rounded-lg bg-slate-elevation2">
      {/* Header */}
      <div className="flex-shrink-0 p-4 pb-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Workflow History</h2>
          <div className="text-sm text-muted-foreground">
            {selectedVersions.size}/2 selected
          </div>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Select up to 2 versions to compare. Current and previous versions are
          selected by default.
        </p>
      </div>

      {/* Compare Buttons */}
      <div className="flex-shrink-0 px-4 pb-3">
        <div className="flex gap-2">
          <Button
            variant="secondary"
            className="flex-1"
            onClick={() => handleCompare("json")}
            disabled={!canCompare || isLoading}
          >
            JSON Diff
          </Button>
          <Button
            className="flex-1"
            onClick={() => handleCompare("visual")}
            disabled={!canCompare || isLoading}
          >
            Visual Compare
          </Button>
        </div>
      </div>

      <Separator />

      {/* Version List */}
      <ScrollArea className="flex-1">
        <div className="p-4">
          {isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <div
                  key={i}
                  className="flex items-center space-x-3 rounded-lg border p-3"
                >
                  <Skeleton className="h-4 w-4" />
                  <div className="flex-1 space-y-2">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-3 w-32" />
                  </div>
                </div>
              ))}
            </div>
          ) : sortedVersions.length === 0 ? (
            <div className="py-8 text-center text-muted-foreground">
              No version history found
            </div>
          ) : (
            <div className="space-y-2">
              {sortedVersions.map((workflow, index) => {
                const isSelected = selectedVersions.has(workflow.version);
                const isCurrent = index === 0;

                return (
                  <div
                    key={workflow.version}
                    className={`flex cursor-pointer items-center space-x-3 rounded-lg border p-3 transition-colors ${
                      isSelected
                        ? "border-primary/20 bg-primary/5"
                        : "hover:bg-muted/50"
                    }`}
                    onClick={() => handleVersionToggle(workflow.version)}
                  >
                    <Checkbox
                      checked={isSelected}
                      onChange={() => {}} // Handled by parent click
                    />

                    <div className="flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">
                          Version {workflow.version}
                        </span>
                        {isCurrent && (
                          <Badge variant="secondary">Current</Badge>
                        )}
                      </div>
                      <div className="text-sm text-muted-foreground">
                        Modified: {basicLocalTimeFormat(workflow.modified_at)}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

export { WorkflowHistoryPanel };
