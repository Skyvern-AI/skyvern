import { create } from "zustand";

type AuthIssue = {
  statusCode: number;
  detail?: string;
  path?: string;
  seenAt: number;
};

type UiSessionFailure = {
  statusCode: number;
  detail?: string;
  seenAt: number;
};

type AuthIssueStore = {
  issue: AuthIssue | null;
  uiSessionFailure: UiSessionFailure | null;
  reportAuthIssue: (issue: Omit<AuthIssue, "seenAt">) => void;
  clearAuthIssue: () => void;
  reportUiSessionFailure: (failure: Omit<UiSessionFailure, "seenAt">) => void;
  clearUiSessionFailure: () => void;
};

const useAuthIssueStore = create<AuthIssueStore>((set) => ({
  issue: null,
  uiSessionFailure: null,
  reportAuthIssue: (issue) => {
    set({ issue: { ...issue, seenAt: Date.now() } });
  },
  clearAuthIssue: () => {
    set((state) => (state.issue === null ? state : { issue: null }));
  },
  reportUiSessionFailure: (failure) => {
    set({ uiSessionFailure: { ...failure, seenAt: Date.now() } });
  },
  clearUiSessionFailure: () => {
    set((state) =>
      state.uiSessionFailure === null ? state : { uiSessionFailure: null },
    );
  },
}));

export { useAuthIssueStore };
export type { AuthIssue, UiSessionFailure };
