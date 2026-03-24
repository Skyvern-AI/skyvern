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
  clearActiveTest: () => void;
};

export type { ActiveCredentialTest };

export const useCredentialTestStore = create<CredentialTestStore>()(
  persist(
    (set) => ({
      activeTest: null,
      setActiveTest: (test) => set({ activeTest: test }),
      clearActiveTest: () => set({ activeTest: null }),
    }),
    {
      name: "credential-test",
      storage: createJSONStorage(() => sessionStorage),
    },
  ),
);
