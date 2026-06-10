import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { Link } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse } from "@/api/types";
import { ToastAction } from "@/components/ui/toast";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  type ActiveBrowserProfileCreate,
  type ActiveCreatePhase,
  useBrowserProfileCreateStore,
} from "@/store/useBrowserProfileCreateStore";

const MAX_TOTAL_DURATION_MS = 5 * 60 * 1000;
const SESSION_POLL_INTERVAL_MS = 5000;
const CREATE_RETRY_INTERVAL_MS = 10000;
const MAX_CONSECUTIVE_ERRORS = 10;

const ARCHIVE_NOT_READY_HINT = "persisted profile archive";
const NOT_OPTED_IN_HINT = "was not configured to generate a browser profile";

type ActiveRefState = ActiveBrowserProfileCreate & {
  timeoutId: ReturnType<typeof setTimeout> | null;
  errorCount: number;
};

type StartBackgroundCreateInput = {
  browserSessionId: string;
  name: string;
  description?: string;
  isSessionRunning: boolean;
};

type CreateBrowserProfilePayload = {
  name: string;
  browser_session_id: string;
  description?: string;
};

function getErrorDetail(error: AxiosError): string | undefined {
  const data = error.response?.data as { detail?: string } | undefined;
  return data?.detail;
}

function useBackgroundBrowserProfileCreate() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const activeRef = useRef<ActiveRefState | null>(null);
  const { setActive, clearActive } = useBrowserProfileCreateStore();

  const cleanup = useCallback(() => {
    if (activeRef.current?.timeoutId) {
      clearTimeout(activeRef.current.timeoutId);
    }
    activeRef.current = null;
    clearActive();
  }, [clearActive]);

  useEffect(() => {
    return () => {
      if (activeRef.current?.timeoutId) {
        clearTimeout(activeRef.current.timeoutId);
        activeRef.current.timeoutId = null;
      }
    };
  }, []);

  const persistPhase = useCallback(
    (phase: ActiveCreatePhase) => {
      if (!activeRef.current) return;
      activeRef.current.phase = phase;
      setActive({
        browserSessionId: activeRef.current.browserSessionId,
        name: activeRef.current.name,
        description: activeRef.current.description,
        startTime: activeRef.current.startTime,
        phase,
      });
    },
    [setActive],
  );

  const tryCreate = useCallback(async () => {
    const active = activeRef.current;
    if (!active) return;

    if (Date.now() - active.startTime > MAX_TOTAL_DURATION_MS) {
      cleanup();
      toast({
        title: "Browser profile creation timed out",
        description:
          "The session archive was not ready within 5 minutes. You can retry from the profiles page.",
        variant: "destructive",
      });
      return;
    }

    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const payload: CreateBrowserProfilePayload = {
        name: active.name,
        browser_session_id: active.browserSessionId,
      };
      if (active.description) {
        payload.description = active.description;
      }
      const response = await client.post<BrowserProfileApiResponse>(
        "/browser_profiles",
        payload,
      );
      const profile = response.data;

      cleanup();
      queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
      queryClient.invalidateQueries({
        queryKey: ["browserProfiles-infinite"],
      });
      toast({
        title: "Browser profile created",
        variant: "success",
        description: `"${profile.name}" was saved from this browser session.`,
        action: (
          <ToastAction altText="View profile" asChild>
            <Link to={`/browser-profiles/${profile.browser_profile_id}`}>
              View profile
            </Link>
          </ToastAction>
        ),
      });
    } catch (error) {
      const axiosError = error as AxiosError;
      const detail = getErrorDetail(axiosError);

      // An opted-out session never uploads an archive, so retrying can't help — fail fast.
      if (
        axiosError.response?.status === 400 &&
        typeof detail === "string" &&
        detail.includes(NOT_OPTED_IN_HINT)
      ) {
        cleanup();
        toast({
          title: "Profile generation not enabled for this session",
          description: detail,
          variant: "destructive",
        });
        return;
      }

      if (
        axiosError.response?.status === 400 &&
        typeof detail === "string" &&
        detail.includes(ARCHIVE_NOT_READY_HINT)
      ) {
        if (activeRef.current) {
          activeRef.current.errorCount = 0;
          activeRef.current.timeoutId = setTimeout(
            tryCreate,
            CREATE_RETRY_INTERVAL_MS,
          );
        }
        return;
      }

      if (!axiosError.response) {
        if (activeRef.current) {
          activeRef.current.errorCount++;
          if (activeRef.current.errorCount >= MAX_CONSECUTIVE_ERRORS) {
            cleanup();
            toast({
              title: "Connection lost",
              description:
                "Unable to finish creating browser profile. Check your network and retry.",
              variant: "destructive",
            });
            return;
          }
          activeRef.current.timeoutId = setTimeout(
            tryCreate,
            CREATE_RETRY_INTERVAL_MS,
          );
        }
        return;
      }

      cleanup();
      toast({
        title: "Failed to create browser profile",
        description: detail ?? axiosError.message,
        variant: "destructive",
      });
    }
  }, [credentialGetter, queryClient, cleanup]);

  const pollUntilClosed = useCallback(async () => {
    const active = activeRef.current;
    if (!active) return;

    if (Date.now() - active.startTime > MAX_TOTAL_DURATION_MS) {
      cleanup();
      toast({
        title: "Browser profile creation timed out",
        description: "The session did not finish closing within 5 minutes.",
        variant: "destructive",
      });
      return;
    }

    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<{ status: string }>(
        `/browser_sessions/${active.browserSessionId}`,
      );

      if (response.data.status === "completed") {
        if (activeRef.current) {
          activeRef.current.errorCount = 0;
          persistPhase("creating");
          activeRef.current.timeoutId = setTimeout(tryCreate, 0);
        }
        return;
      }

      if (response.data.status === "failed") {
        cleanup();
        toast({
          title: "Browser session failed",
          description:
            "Could not create profile — the session ended in a failed state.",
          variant: "destructive",
        });
        return;
      }

      if (activeRef.current) {
        activeRef.current.errorCount = 0;
        activeRef.current.timeoutId = setTimeout(
          pollUntilClosed,
          SESSION_POLL_INTERVAL_MS,
        );
      }
    } catch {
      if (activeRef.current) {
        activeRef.current.errorCount++;
        if (activeRef.current.errorCount >= MAX_CONSECUTIVE_ERRORS) {
          cleanup();
          toast({
            title: "Connection lost",
            description: "Unable to track browser session status.",
            variant: "destructive",
          });
          return;
        }
        activeRef.current.timeoutId = setTimeout(
          pollUntilClosed,
          SESSION_POLL_INTERVAL_MS,
        );
      }
    }
  }, [credentialGetter, cleanup, persistPhase, tryCreate]);

  const rehydratedRef = useRef(false);
  useEffect(() => {
    if (rehydratedRef.current) return;
    rehydratedRef.current = true;

    const stored = useBrowserProfileCreateStore.getState().active;
    if (!stored || activeRef.current) return;

    if (Date.now() - stored.startTime > MAX_TOTAL_DURATION_MS) {
      clearActive();
      return;
    }

    activeRef.current = {
      ...stored,
      timeoutId: null,
      errorCount: 0,
    };
    activeRef.current.timeoutId =
      stored.phase === "creating"
        ? setTimeout(tryCreate, CREATE_RETRY_INTERVAL_MS)
        : setTimeout(pollUntilClosed, SESSION_POLL_INTERVAL_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once on mount
  }, []);

  const startBackgroundCreate = useCallback(
    async ({
      browserSessionId,
      name,
      description,
      isSessionRunning,
    }: StartBackgroundCreateInput) => {
      cleanup();

      const startTime = Date.now();
      const initialPhase: ActiveCreatePhase = isSessionRunning
        ? "closing"
        : "creating";

      activeRef.current = {
        browserSessionId,
        name,
        description,
        startTime,
        phase: initialPhase,
        timeoutId: null,
        errorCount: 0,
      };
      setActive({
        browserSessionId,
        name,
        description,
        startTime,
        phase: initialPhase,
      });

      toast({
        title: "Creating browser profile",
        description: isSessionRunning
          ? "Closing the session and capturing its state. When done, you'll see it in Browser Profiles."
          : "Capturing the session's state. When done, you'll see it in Browser Profiles.",
      });

      if (!isSessionRunning) {
        activeRef.current.timeoutId = setTimeout(tryCreate, 0);
        return;
      }

      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        // Opt the running session into profile generation before closing so teardown uploads its
        // profile — sessions default to not persisting one.
        await client.patch(`/browser_sessions/${browserSessionId}`, {
          generate_browser_profile: true,
        });
        await client.post(`/browser_sessions/${browserSessionId}/close`);
        queryClient.invalidateQueries({
          queryKey: ["browserSession", browserSessionId],
        });
        queryClient.invalidateQueries({ queryKey: ["browserSessions"] });

        if (activeRef.current) {
          persistPhase("waiting");
          activeRef.current.timeoutId = setTimeout(
            pollUntilClosed,
            SESSION_POLL_INTERVAL_MS,
          );
        }
      } catch (error) {
        cleanup();
        const axiosError = error as AxiosError;
        const detail = getErrorDetail(axiosError);
        toast({
          title: "Failed to close browser session",
          description:
            detail ?? "Could not close the session to capture its state.",
          variant: "destructive",
        });
      }
    },
    [
      credentialGetter,
      queryClient,
      cleanup,
      persistPhase,
      pollUntilClosed,
      tryCreate,
      setActive,
    ],
  );

  return { startBackgroundCreate };
}

export type { StartBackgroundCreateInput };
export { useBackgroundBrowserProfileCreate };
