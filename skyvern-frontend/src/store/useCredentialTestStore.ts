import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type ActiveCredentialTest = {
  credentialId: string;
  workflowRunId: string;
  url: string;
  startTime: number;
};

type CredentialTestStore = {
  activeTest: ActiveCredentialTest | null;
  setActiveTest: (test: ActiveCredentialTest) => void;
  /** With a run id, clears only if the slot still holds that run (another tab may have started a newer test). */
  clearActiveTest: (workflowRunId?: string) => void;
};

export type { ActiveCredentialTest };

export const useCredentialTestStore = create<CredentialTestStore>()(
  persist(
    (set) => ({
      activeTest: null,
      setActiveTest: (test) => set({ activeTest: test }),
      clearActiveTest: (workflowRunId) =>
        set((state) =>
          workflowRunId && state.activeTest?.workflowRunId !== workflowRunId
            ? state
            : { activeTest: null },
        ),
    }),
    {
      name: "credential-test",
      // localStorage, not sessionStorage: the watch-run link opens a noopener tab
      // whose sessionStorage starts empty, which hid in-flight tests from that tab.
      storage: createJSONStorage(() => localStorage),
    },
  ),
);

// Sync the shared slot across tabs; otherwise a tab's persist write (e.g. on its
// own test finishing) would silently clobber a newer test started in another tab.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === "credential-test") {
      void useCredentialTestStore.persist.rehydrate();
    }
  });
}
