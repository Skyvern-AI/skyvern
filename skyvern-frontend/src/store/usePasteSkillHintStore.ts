import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type PasteSkillHintState = {
  dismissed: boolean;
  dismiss: () => void;
};

export const PASTE_SKILL_HINT_STORAGE_KEY = "skyvern.copilot.pasteSkillHint";

export const usePasteSkillHintStore = create<PasteSkillHintState>()(
  persist(
    (set) => ({
      dismissed: false,
      dismiss: () => set({ dismissed: true }),
    }),
    {
      name: PASTE_SKILL_HINT_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ dismissed: state.dismissed }),
    },
  ),
);
