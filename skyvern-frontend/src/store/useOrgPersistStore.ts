import { create } from "zustand";
import { persist } from "zustand/middleware";

type OrgPersistState = {
  /** Map of userId → org bootstrap has already run */
  hasInitializedByUser: Record<string, boolean>;
  /** Set to true once the persist middleware has finished rehydrating from localStorage */
  _hasHydrated: boolean;
  markInitialized: (userId: string) => void;
  hasInitialized: (userId: string) => boolean;
};

const useOrgPersistStore = create<OrgPersistState>()(
  persist(
    (set, get) => ({
      hasInitializedByUser: {},
      _hasHydrated: false,
      markInitialized: (userId) =>
        set((state) => ({
          hasInitializedByUser: state.hasInitializedByUser[userId]
            ? state.hasInitializedByUser
            : { ...state.hasInitializedByUser, [userId]: true },
        })),
      hasInitialized: (userId) => Boolean(get().hasInitializedByUser[userId]),
    }),
    {
      name: "skyvern-org-persist",
      onRehydrateStorage: () => () => {
        useOrgPersistStore.setState({ _hasHydrated: true });
      },
      partialize: (state) => ({
        hasInitializedByUser: state.hasInitializedByUser,
      }),
    },
  ),
);

export { useOrgPersistStore };
