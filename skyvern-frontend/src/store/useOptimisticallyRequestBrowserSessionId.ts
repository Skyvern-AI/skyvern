import { AxiosInstance } from "axios";
import { create as createStore } from "zustand";

import { User } from "@/api/types";
import { lsKeys } from "@/util/env";

export interface BrowserSessionData {
  browser_session_id: string | null;
  expires_at: number | null; // seconds since epoch
}

interface RunOpts {
  client: AxiosInstance;
  reason?: string;
  user: User;
  workflowPermanentId?: string;
}

export interface OptimisticBrowserSession {
  get: (user: User, workflowPermanentId: string) => BrowserSessionData | null;
  run: (runOpts: RunOpts) => Promise<BrowserSessionData>;
}

const SESSION_TIMEOUT_MINUTES = 60;
const SPARE = "spare";

const makeKey = (user: User, workflowPermanentId?: string | undefined) => {
  return `${lsKeys.optimisticBrowserSession}:${user.id}:${workflowPermanentId ?? SPARE}`;
};

/**
 * Read a `BrowserSessionData` from localStorage cache. If the entry is expired,
 * return `null`. If the entry is invalid, return `null`. Otherwise return it.
 */
const read = (key: string): BrowserSessionData | null => {
  const stored = localStorage.getItem(key);
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
        return { browser_session_id, expires_at };
      }
    } catch (e) {
      // pass
    }
  }

  return null;
};

/**
 * Write a `BrowserSessionData` to localStorage cache.
 */
const write = (key: string, browserSessionData: BrowserSessionData) => {
  localStorage.setItem(key, JSON.stringify(browserSessionData));
};

/**
 * Delete a localStorage key.
 */
const del = (key: string) => {
  localStorage.removeItem(key);
};

/**
 * Create a new browser session and return the `BrowserSessionData`.
 */
const create = async (client: AxiosInstance): Promise<BrowserSessionData> => {
  const resp = await client.post("/browser_sessions", {
    timeout: SESSION_TIMEOUT_MINUTES,
  });

  const { browser_session_id: newBrowserSessionId, timeout } = resp.data;
  const newExpiresAt = Math.floor(Date.now() / 1000) + timeout * 60 * 0.9;

  return {
    browser_session_id: newBrowserSessionId,
    expires_at: newExpiresAt,
  };
};

export const useOptimisticallyRequestBrowserSessionId =
  createStore<OptimisticBrowserSession>(() => ({
    get: (user: User, workflowPermanentId: string) => {
      return read(makeKey(user, workflowPermanentId));
    },
    run: async ({ client, user, workflowPermanentId }: RunOpts) => {
      if (workflowPermanentId) {
        const userKey = makeKey(user, workflowPermanentId);
        const exists = read(userKey);

        if (exists) {
          return exists;
        }

        const spareKey = makeKey(user, SPARE);
        const spare = read(spareKey);

        if (spare) {
          del(spareKey);
          write(userKey, spare);
          create(client).then((newSpare) => write(spareKey, newSpare));
          return spare;
        }
      }

      const key = makeKey(user, workflowPermanentId);
      const browserSessionData = read(key);

      if (browserSessionData) {
        return browserSessionData;
      }

      const knew = await create(client);
      write(key, knew);

      return knew;
    },
  }));
