import { create } from "zustand";

import { type WorkflowCopilotChatSummary } from "@/routes/workflows/copilot/workflowCopilotTypes";

export type CopilotHeaderControls = {
  workflowPermanentId: string | undefined;
  currentChatId: string | null;
  onSelectChat: (chat: WorkflowCopilotChatSummary) => void;
  onNewChat: () => void;
  disabled: boolean;
  newChatDisabled: boolean;
  // Why chat navigation is locked (an Accept whose outcome is unresolved), or null.
  navigationLockedReason: string | null;
};

// What the docked Copilot chat is waiting on the user for.
export type CopilotAttention = "question" | "credential" | "account";

export const COPILOT_ATTENTION_LABEL: Record<CopilotAttention, string> = {
  question: "waiting for your answer",
  credential: "needs to sign in",
  account: "needs a Google account",
};

export const COPILOT_ATTENTION_DOT: Record<CopilotAttention, string> = {
  question: "Copilot needs your answer",
  credential: "Copilot needs to sign in",
  account: "Copilot needs a Google account",
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
  // The chat stays mounted while its pane is closed, so the studio's pane toggle can flag it.
  attention: CopilotAttention | null;
  setAttention: (attention: CopilotAttention | null) => void;
};

export const useCopilotHeaderStore = create<CopilotHeaderState>((set) => ({
  controls: null,
  setControls: (controls) => set({ controls }),
  attention: null,
  setAttention: (attention) => set({ attention }),
}));
