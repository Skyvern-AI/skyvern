import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import type {
  CredentialApiResponse,
  TestCredentialStatusResponse,
  TestLoginResponse,
} from "@/api/types";
import { getHostname } from "@/util/getHostname";
import { useCredentialTestStore } from "@/store/useCredentialTestStore";

const MAX_POLL_DURATION_MS = 5 * 60 * 1000;
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
 * Active test state is persisted in a zustand store backed by sessionStorage
 * so it survives both SPA navigation and full page reloads. On mount, the hook
 * checks for persisted state and resumes polling if a test was in progress.
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
    if (activeTestRef.current?.timeoutId) {
      clearTimeout(activeTestRef.current.timeoutId);
    }
    activeTestRef.current = null;
    clearActiveTest();
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


  const updateCredentialBrowserProfileInCache = useCallback(
    (
      credentialId: string,
      browserProfileId?: string | null,
      testedUrl?: string | null,
    ) => {
      queryClient.setQueriesData<Array<CredentialApiResponse>>(
        { queryKey: ["credentials"] },
        (credentials) => {
          if (!credentials) {
            return credentials;
          }

          let didUpdate = false;
          const nextCredentials = credentials.map((credential) => {
            if (credential.credential_id !== credentialId) {
              return credential;
            }

            didUpdate = true;
            return {
              ...credential,
              ...(browserProfileId ? { browser_profile_id: browserProfileId } : {}),
              ...(testedUrl ? { tested_url: testedUrl } : {}),
            };
          });

          return didUpdate ? nextCredentials : credentials;
        },
      );
    },
    [queryClient],
  );

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
          "The test did not complete within 5 minutes. Your credential is saved but without a browser profile.",
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

      // Reset error counter on successful poll
      if (activeTestRef.current) {
        activeTestRef.current.errorCount = 0;
      }

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
        if (data.browser_profile_id) {
          updateCredentialBrowserProfileInCache(
            data.credential_id,
            data.browser_profile_id,
            data.tested_url,
          );
        }
        await queryClient.invalidateQueries({ queryKey: ["credentials"] });

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
        await queryClient.invalidateQueries({ queryKey: ["credentials"] });
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
  }, [
    credentialGetter,
    queryClient,
    cleanup,
    updateCredentialBrowserProfileInCache,
  ]);

  // Rehydrate polling from persisted store after a full page reload.
  // If a test was in progress before the reload, resume polling so the
  // link stays visible and the toast fires when the test finishes.
  const rehydratedRef = useRef(false);
  useEffect(() => {
    if (rehydratedRef.current) return;
    rehydratedRef.current = true;

    const stored = useCredentialTestStore.getState().activeTest;
    if (!stored || activeTestRef.current) return;

    // If the persisted test already exceeded the timeout, clean up
    if (Date.now() - stored.startTime > MAX_POLL_DURATION_MS) {
      clearActiveTest();
      return;
    }

    activeTestRef.current = {
      credentialId: stored.credentialId,
      workflowRunId: stored.workflowRunId,
      url: stored.url,
      startTime: stored.startTime,
      timeoutId: setTimeout(poll, POLL_INTERVAL_MS),
      errorCount: 0,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally runs once on mount
  }, []);

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
