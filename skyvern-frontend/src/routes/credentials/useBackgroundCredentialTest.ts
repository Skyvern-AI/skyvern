import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import type {
  TestCredentialStatusResponse,
  TestLoginResponse,
} from "@/api/types";
import { getHostname } from "@/util/getHostname";
import { useCredentialTestStore } from "@/store/useCredentialTestStore";

const MAX_POLL_DURATION_MS = 10 * 60 * 1000;
const POLL_INTERVAL_MS = 5000;
const MAX_CONSECUTIVE_ERRORS = 10;

type ActiveTest = {
  credentialId: string;
  workflowRunId: string;
  url: string;
  startTime: number;
  timeoutId: ReturnType<typeof setTimeout> | null;
  errorCount: number;
};

/**
 * Hook that manages background credential browser-profile tests.
 *
 * After a credential is saved with "Save browser session" checked,
 * call `startBackgroundTest(credentialId, url, userContext?)` to kick off an async test.
 * The hook polls the backend, shows toast notifications on completion/failure,
 * and invalidates the credentials query so the list updates.
 *
 * Active test state is persisted in a zustand store backed by localStorage so
 * it survives reloads and is visible across tabs (e.g. the noopener watch-run
 * tab). The hook adopts the persisted test on mount and whenever the slot
 * changes (a test started in another tab), resuming polling either way.
 *
 * Instantiate this in a component that outlives the modal (e.g. CredentialsPage)
 * so polling survives modal close.
 */
function useBackgroundCredentialTest() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const activeTestRef = useRef<ActiveTest | null>(null);
  const { setActiveTest, clearActiveTest } = useCredentialTestStore();

  // Full cleanup: stop polling AND clear the persisted store.
  // Used when a test reaches a terminal state (completed/failed/timeout).
  const cleanup = useCallback(() => {
    const current = activeTestRef.current;
    if (current?.timeoutId) {
      clearTimeout(current.timeoutId);
    }
    activeTestRef.current = null;
    // Scoped to this tab's run so it can't wipe a newer test from another tab.
    clearActiveTest(current?.workflowRunId);
  }, [clearActiveTest]);

  // On unmount, only stop the timer — don't clear the store so the test
  // link survives SPA navigation. Polling resumes via rehydration on remount.
  useEffect(() => {
    return () => {
      if (activeTestRef.current?.timeoutId) {
        clearTimeout(activeTestRef.current.timeoutId);
        activeTestRef.current.timeoutId = null;
      }
    };
  }, []);

  const poll = useCallback(async () => {
    const test = activeTestRef.current;
    if (!test) return;

    if (Date.now() - test.startTime > MAX_POLL_DURATION_MS) {
      // Cancel the backend workflow run so it stops consuming resources
      const { credentialId, workflowRunId } = test;
      cleanup();
      getClient(credentialGetter, "sans-api-v1")
        .then((client) =>
          client.post(
            `/credentials/${credentialId}/test/${workflowRunId}/cancel`,
          ),
        )
        .catch(() => {
          // Best-effort — backend timeout will eventually clean up
        });
      toast({
        title: "Browser profile test timed out",
        description:
          "The test did not complete within 10 minutes. Your credential is saved but without a browser profile.",
        variant: "destructive",
      });
      return;
    }

    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<TestCredentialStatusResponse>(
        `/credentials/${test.credentialId}/test/${test.workflowRunId}`,
      );
      const data = response.data;

      // A newer test may have superseded this run while the request was in
      // flight — its own poll chain owns the state now.
      if (activeTestRef.current?.workflowRunId !== test.workflowRunId) {
        return;
      }
      activeTestRef.current.errorCount = 0;

      if (data.status === "completed") {
        // The backend sets browser_profile_id in a separate background task
        // AFTER the workflow completes. If the profile isn't ready yet and
        // no failure reason has been reported, keep polling.
        if (!data.browser_profile_id && !data.browser_profile_failure_reason) {
          if (activeTestRef.current) {
            activeTestRef.current.timeoutId = setTimeout(
              poll,
              POLL_INTERVAL_MS,
            );
          }
          return;
        }

        cleanup();
        queryClient.invalidateQueries({ queryKey: ["credentials"] });

        if (data.browser_profile_failure_reason && !data.browser_profile_id) {
          toast({
            title: "Browser profile was not saved",
            description: data.browser_profile_failure_reason,
            variant: "destructive",
          });
        } else {
          const host = data.tested_url ? getHostname(data.tested_url) : null;
          toast({
            title: "Browser profile test passed",
            description: host
              ? `Saved browser session enabled for ${host}`
              : "Saved browser session enabled.",
            variant: "success",
          });
        }
        return;
      }

      if (
        data.status === "failed" ||
        data.status === "terminated" ||
        data.status === "timed_out" ||
        data.status === "canceled"
      ) {
        cleanup();
        queryClient.invalidateQueries({ queryKey: ["credentials"] });
        const host = test.url ? getHostname(test.url) : null;
        toast({
          title: host
            ? `Unable to save browser session for ${host}`
            : "Unable to save browser session",
          description:
            data.failure_reason ??
            "The login test did not succeed. Your credential is saved but without a browser profile.",
          variant: "destructive",
        });
        return;
      }

      // Still running — schedule next poll (guard against unmount race)
      if (activeTestRef.current) {
        activeTestRef.current.timeoutId = setTimeout(poll, POLL_INTERVAL_MS);
      }
    } catch {
      if (activeTestRef.current?.workflowRunId !== test.workflowRunId) {
        return;
      }
      // Network error — increment counter and bail if too many consecutive failures
      if (activeTestRef.current) {
        activeTestRef.current.errorCount++;
        if (activeTestRef.current.errorCount >= MAX_CONSECUTIVE_ERRORS) {
          cleanup();
          toast({
            title: "Connection lost",
            description:
              "Unable to check browser profile test status. Your credential is saved — check back later to see if the profile was created.",
            variant: "destructive",
          });
          return;
        }
        activeTestRef.current.timeoutId = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }
  }, [credentialGetter, queryClient, cleanup]);

  // Adopt the persisted test whenever this tab isn't already polling it —
  // covers mount rehydration AND tests started in other tabs (storage sync).
  const storedTest = useCredentialTestStore((s) => s.activeTest);
  useEffect(() => {
    if (!storedTest) return;

    const current = activeTestRef.current;
    if (
      current?.workflowRunId === storedTest.workflowRunId &&
      current.timeoutId !== null
    ) {
      return;
    }

    // If the persisted test already exceeded the timeout, clean up
    if (Date.now() - storedTest.startTime > MAX_POLL_DURATION_MS) {
      clearActiveTest(storedTest.workflowRunId);
      return;
    }

    if (current?.timeoutId) {
      clearTimeout(current.timeoutId);
    }
    activeTestRef.current = {
      credentialId: storedTest.credentialId,
      workflowRunId: storedTest.workflowRunId,
      url: storedTest.url,
      startTime: storedTest.startTime,
      // Poll immediately, not after a full interval: the test may have already
      // finished, and the credentials list shouldn't sit stale any longer.
      timeoutId: setTimeout(poll, 0),
      errorCount: 0,
    };
  }, [storedTest, poll, clearActiveTest]);

  const startBackgroundTest = useCallback(
    async (credentialId: string, url: string, userContext?: string) => {
      // Clean up any previous test
      cleanup();

      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.post<TestLoginResponse>(
          `/credentials/${credentialId}/test`,
          {
            url,
            save_browser_profile: true,
            user_context: userContext?.trim() || null,
          },
        );
        const data = response.data;

        const startTime = Date.now();
        activeTestRef.current = {
          credentialId,
          workflowRunId: data.workflow_run_id,
          url,
          startTime,
          timeoutId: setTimeout(poll, POLL_INTERVAL_MS),
          errorCount: 0,
        };
        setActiveTest({
          credentialId,
          workflowRunId: data.workflow_run_id,
          url,
          startTime,
        });
      } catch {
        toast({
          title: "Failed to start browser profile test",
          description:
            "The credential was saved but the test could not be started.",
          variant: "destructive",
        });
      }
    },
    [credentialGetter, cleanup, poll, setActiveTest],
  );

  return { startBackgroundTest };
}

export { useBackgroundCredentialTest };
