import { create } from "zustand";

import { type WorkflowCopilotChatSummary } from "@/routes/workflows/copilot/workflowCopilotTypes";

export type CopilotHeaderControls = {
  workflowPermanentId: string | undefined;
  currentChatId: string | null;
  onSelectChat: (chat: WorkflowCopilotChatSummary) => void;
  onNewChat: () => void;
  disabled: boolean;
};

/**
 * Bridges the docked copilot chat and the studio's Copilot pane header: the
 * chat registers its History/New-chat controls here so the header (rendered
 * by StudioShell, outside the chat's tree) can host them. Null when no docked
 * chat is mounted — the header then renders no controls.
 */
type CopilotHeaderState = {
  controls: CopilotHeaderControls | null;
  setControls: (controls: CopilotHeaderControls | null) => void;
};

export const useCopilotHeaderStore = create<CopilotHeaderState>((set) => ({
  controls: null,
  setControls: (controls) => set({ controls }),
}));
