import { create } from "zustand";

// request changes on every ask, so asking again for the same trigger reopens it.
export const useTriggerFocusStore = create<{
  triggerId: string | null;
  request: number;
}>(() => ({ triggerId: null, request: 0 }));
