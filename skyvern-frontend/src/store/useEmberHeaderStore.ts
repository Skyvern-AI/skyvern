import { create } from "zustand";

import { type EmberChatSummary } from "@/routes/workflows/ember/emberTypes";

export type EmberHeaderControls = {
  workflowPermanentId: string | undefined;
  currentChatId: string | null;
  onSelectChat: (chat: EmberChatSummary) => void;
  onNewChat: () => void;
  disabled: boolean;
  newChatDisabled: boolean;
  // Why chat navigation is locked (an Accept whose outcome is unresolved), or null.
  navigationLockedReason: string | null;
};

// What the docked Ember chat is waiting on the user for.
export type EmberAttention =
  | "question"
  | "credential"
  | "account"
  | "demonstration";

export const EMBER_ATTENTION_LABEL: Record<EmberAttention, string> = {
  question: "waiting for your answer",
  credential: "needs to sign in",
  account: "needs an account",
  demonstration: "waiting for your demonstration",
};

export const EMBER_ATTENTION_DOT: Record<EmberAttention, string> = {
  question: "Ember needs your answer",
  credential: "Ember needs to sign in",
  account: "Ember needs an account",
  demonstration: "Ember is watching your demonstration",
};

/**
 * Bridges the docked Ember chat and the studio's Ember pane header: the
 * chat registers its History/New-chat controls here so the header (rendered
 * by StudioShell, outside the chat's tree) can host them. Null when no docked
 * chat is mounted — the header then renders no controls.
 */
type EmberHeaderState = {
  controls: EmberHeaderControls | null;
  setControls: (controls: EmberHeaderControls | null) => void;
  // The chat stays mounted while its pane is closed, so the studio's pane toggle can flag it.
  attention: EmberAttention | null;
  setAttention: (attention: EmberAttention | null) => void;
};

export const useEmberHeaderStore = create<EmberHeaderState>((set) => ({
  controls: null,
  setControls: (controls) => set({ controls }),
  attention: null,
  setAttention: (attention) => set({ attention }),
}));
