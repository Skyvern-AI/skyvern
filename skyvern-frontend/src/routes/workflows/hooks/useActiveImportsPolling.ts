import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/use-toast";

export type ActiveImport = {
  import_id: string;
  status: "importing" | "completed" | "failed";
  file_name: string | null;
  workflow_id: string | null;
  error: string | null;
  created_at: string;
};

export function useActiveImportsPolling() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const previousCountRef = useRef<number>(0);
  const [shouldPoll, setShouldPoll] = useState(true); // Start with true to check on mount
  const justStartedPollingRef = useRef(false); // Prevent immediate stop after starting
  const seenFailuresRef = useRef<Set<string>>(new Set()); // Track failures we've already shown
  const seenCompletionsRef = useRef<Set<string>>(new Set()); // Track completions we've already shown
  const previousImportsRef = useRef<Map<string, ActiveImport>>(new Map()); // Track previous state

  const { data: activeImports = [] } = useQuery({
    queryKey: ["active-imports"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<ActiveImport[]>("/workflows/active-imports");
      return response.data;
    },
    enabled: shouldPoll, // Only run query when shouldPoll is true
    refetchInterval: shouldPoll ? 3000 : false, // Only poll when shouldPoll is true
    refetchIntervalInBackground: true,
    staleTime: 0, // Always consider data stale so it refetches immediately
  });

  // Monitor for completed/failed imports and invalidate workflows when count decreases
  useEffect(() => {
    const currentCount = activeImports.length;
    const previousCount = previousCountRef.current;
    const importingCount = activeImports.filter((imp) => imp.status === "importing").length;

    console.log(`📊 Active imports: ${currentCount} (importing: ${importingCount}, previous: ${previousCount}), polling: ${shouldPoll}, justStarted: ${justStartedPollingRef.current}`);

    // Check for status changes (importing -> failed/completed)
    activeImports.forEach((imp) => {
      const previousImport = previousImportsRef.current.get(imp.import_id);
      
      // Failed import (new failure or status changed to failed)
      if (imp.status === "failed" && !seenFailuresRef.current.has(imp.import_id)) {
        console.log(`❌ Import failed: ${imp.import_id}`, imp.error);
        seenFailuresRef.current.add(imp.import_id);
        
        toast({
          variant: "destructive",
          title: "Import failed",
          description: imp.error || `Failed to import ${imp.file_name || "workflow"}`,
        });
        
        // Refresh workflows to remove placeholder
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
      }
      
      // Completed import (status changed from "importing" to "completed")
      if (
        imp.status === "completed" && 
        previousImport?.status === "importing" &&
        !seenCompletionsRef.current.has(imp.import_id)
      ) {
        console.log(`✅ Import completed: ${imp.import_id}`);
        seenCompletionsRef.current.add(imp.import_id);
        
        toast({
          variant: "success",
          title: "Workflow imported",
          description: `Successfully imported ${imp.file_name || "workflow"}`,
        });
        
        // Refresh workflows to show new workflow
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
      }
    });
    
    // Update previous imports map for next comparison
    previousImportsRef.current = new Map(
      activeImports.map((imp) => [imp.import_id, imp])
    );

    // If we have active IMPORTING imports, make sure polling is enabled
    if (importingCount > 0 && !shouldPoll) {
      console.log("🔄 Active imports detected, resuming polling");
      setShouldPoll(true);
    }

    // Stop polling if there are no IMPORTING imports (completed/failed will age out in 10s)
    // BUT don't stop if we just started polling (to avoid race condition)
    if (importingCount === 0 && shouldPoll && !justStartedPollingRef.current) {
      console.log("⏸️ No importing imports, stopping polling");
      setShouldPoll(false);
    }

    // Clear the "just started" flag once we have imports or if count changed
    if (justStartedPollingRef.current && (currentCount > 0 || currentCount !== previousCount)) {
      console.log("🔓 Clearing justStartedPolling flag");
      justStartedPollingRef.current = false;
    }

    previousCountRef.current = currentCount;
  }, [activeImports, queryClient, shouldPoll]);

  // Function to start polling (called when a new import starts)
  const startPolling = () => {
    console.log("🚀 Starting import polling");
    justStartedPollingRef.current = true; // Set flag to prevent immediate stop
    setShouldPoll(true);
    // Force refetch immediately to get the latest state
    queryClient.refetchQueries({ queryKey: ["active-imports"] });
  };

  return { activeImports, startPolling };
}

