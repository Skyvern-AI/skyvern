import { create } from "zustand";

type MissingStartUrlState = {
  // Blocks whose run was refused for a missing starting URL; their editors
  // highlight the URL field until one is entered.
  flaggedBlockIds: ReadonlySet<string>;
  flag: (blockId: string) => void;
  clear: (blockId: string) => void;
};

export const useMissingStartUrlStore = create<MissingStartUrlState>((set) => ({
  flaggedBlockIds: new Set(),
  flag: (blockId) =>
    set((state) => ({
      flaggedBlockIds: new Set(state.flaggedBlockIds).add(blockId),
    })),
  clear: (blockId) =>
    set((state) => {
      if (!state.flaggedBlockIds.has(blockId)) return state;
      const next = new Set(state.flaggedBlockIds);
      next.delete(blockId);
      return { flaggedBlockIds: next };
    }),
}));
