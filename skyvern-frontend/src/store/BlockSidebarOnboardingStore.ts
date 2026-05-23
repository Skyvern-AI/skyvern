import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export const BLOCK_SIDEBAR_ONBOARDING_STORAGE_KEY =
  "skyvern.blockSidebarOnboarded";

type BlockSidebarOnboardingStore = {
  hasSeenMigration: boolean;
  markSeen: () => void;
  reset: () => void;
};

const useBlockSidebarOnboardingStore = create<BlockSidebarOnboardingStore>()(
  persist(
    (set) => ({
      hasSeenMigration: false,
      markSeen: () => set({ hasSeenMigration: true }),
      reset: () => set({ hasSeenMigration: false }),
    }),
    {
      name: BLOCK_SIDEBAR_ONBOARDING_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ hasSeenMigration: state.hasSeenMigration }),
    },
  ),
);

export { useBlockSidebarOnboardingStore };
