import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/use-toast";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { useActiveImportsQuery } from "./useActiveImportsQuery";

export function useActiveImportsPolling() {
  const queryClient = useQueryClient();
  const previousCountRef = useRef<number>(0);
  const [shouldPoll, setShouldPoll] = useState(true); // Start with true to check on mount
  const justStartedPollingRef = useRef(false); // Prevent immediate stop after starting
  const seenFailuresRef = useRef<Set<string>>(new Set()); // Track failures we've already shown
  const seenCompletionsRef = useRef<Set<string>>(new Set()); // Track completions we've already shown
  const previousImportsRef = useRef<Map<string, WorkflowApiResponse>>(
    new Map(),
  ); // Track previous state

  const { data: activeImports = [] } = useActiveImportsQuery({
    enabled: shouldPoll,
    refetchInterval: shouldPoll ? 3000 : false,
  });

  // Monitor for completed/failed imports and invalidate workflows when count decreases
  useEffect(() => {
    const currentCount = activeImports.length;
    const previousCount = previousCountRef.current;
    const importingCount = activeImports.filter(
      (imp) => imp.status === "importing",
    ).length;

    // Reset completion/failure tracking when an import restarts
    activeImports.forEach((imp) => {
      if (imp.status === "importing") {
        seenCompletionsRef.current.delete(imp.workflow_permanent_id);
        seenFailuresRef.current.delete(imp.workflow_permanent_id);
      }
    });

    // Check for status changes and disappeared workflows
    const currentPermanentIds = new Set(
      activeImports.map((imp) => imp.workflow_permanent_id),
    );

    // Check for workflows that disappeared (importing -> completed successfully)
    previousImportsRef.current.forEach((prevImport, permanentId) => {
      // If it was importing and now it's gone, it completed successfully!
      if (
        prevImport.status === "importing" &&
        !currentPermanentIds.has(permanentId) &&
        !seenCompletionsRef.current.has(permanentId)
      ) {
        seenCompletionsRef.current.add(permanentId);

        toast({
          variant: "success",
          title: "Workflow imported",
          description: `Successfully imported ${prevImport.title || "workflow"}`,
        });

        // Refresh workflows and folders to show new workflow and update folder counts
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      }
    });

    // Check for failed imports (status changed from importing → import_failed)
    activeImports.forEach((imp) => {
      const previousImport = previousImportsRef.current.get(
        imp.workflow_permanent_id,
      );

      // Only show toast if we SAW the transition from importing → import_failed
      if (
        imp.status === "import_failed" &&
        previousImport?.status === "importing" &&
        !seenFailuresRef.current.has(imp.workflow_permanent_id)
      ) {
        seenFailuresRef.current.add(imp.workflow_permanent_id);

        toast({
          variant: "destructive",
          title: "Import failed",
          description:
            imp.import_error || `Failed to import ${imp.title || "workflow"}`,
        });

        // Refresh workflows to update UI
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
      }
    });

    // Update previous imports map for next comparison
    previousImportsRef.current = new Map(
      activeImports.map((imp) => [imp.workflow_permanent_id, imp]),
    );

    // If we have active IMPORTING imports, make sure polling is enabled
    if (importingCount > 0 && !shouldPoll) {
      setShouldPoll(true);
    }

    // Stop polling if there are no IMPORTING imports
    // BUT don't stop if we just started polling (to avoid race condition)
    if (importingCount === 0 && shouldPoll && !justStartedPollingRef.current) {
      setShouldPoll(false);
    }

    // Clear the "just started" flag once we have imports or if count changed
    if (
      justStartedPollingRef.current &&
      (currentCount > 0 || currentCount !== previousCount)
    ) {
      justStartedPollingRef.current = false;
    }

    previousCountRef.current = currentCount;
  }, [activeImports, queryClient, shouldPoll]);

  // Function to start polling (called when a new import starts)
  const startPolling = () => {
    justStartedPollingRef.current = true; // Set flag to prevent immediate stop
    setShouldPoll(true);
    // Force refetch immediately to get the latest state
    queryClient.refetchQueries({ queryKey: ["active-imports"] });
    queryClient.refetchQueries({ queryKey: ["workflows"] });
  };

  return { activeImports, startPolling };
}
