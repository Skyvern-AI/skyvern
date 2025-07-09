import { create } from "zustand";
import { AxiosInstance } from "axios";
import { lsKeys } from "@/util/env";

export interface BrowserSessionData {
  browser_session_id: string | null;
  expires_at: number | null; // seconds since epoch
}

interface OptimisticBrowserSessionIdState extends BrowserSessionData {
  run: (client: AxiosInstance) => Promise<BrowserSessionData>;
}

const SESSION_TIMEOUT_MINUTES = 60;

export const useOptimisticallyRequestBrowserSessionId =
  create<OptimisticBrowserSessionIdState>((set) => ({
    browser_session_id: null,
    expires_at: null,
    run: async (client) => {
      const stored = localStorage.getItem(lsKeys.optimisticBrowserSession);
      if (stored) {
        try {
          const parsed = JSON.parse(stored);
          const { browser_session_id, expires_at } = parsed;
          const now = Math.floor(Date.now() / 1000); // seconds since epoch

          if (
            browser_session_id &&
            typeof browser_session_id === "string" &&
            expires_at &&
            typeof expires_at === "number" &&
            now < expires_at
          ) {
            set({ browser_session_id, expires_at });
            return { browser_session_id, expires_at };
          }
        } catch (e) {
          // pass
        }
      }

      const resp = await client.post("/browser_sessions", {
        timeout: SESSION_TIMEOUT_MINUTES,
      });
      const { browser_session_id: newBrowserSessionId, timeout } = resp.data;
      const newExpiresAt = Math.floor(Date.now() / 1000) + timeout * 60 * 0.9;
      set({
        browser_session_id: newBrowserSessionId,
        expires_at: newExpiresAt,
      });
      localStorage.setItem(
        lsKeys.optimisticBrowserSession,
        JSON.stringify({
          browser_session_id: newBrowserSessionId,
          expires_at: newExpiresAt,
        }),
      );

      return {
        browser_session_id: newBrowserSessionId,
        expires_at: newExpiresAt,
      };
    },
  }));
