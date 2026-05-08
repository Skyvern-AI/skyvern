import { create } from "zustand";

type AuthIssue = {
  statusCode: number;
  detail?: string;
  path?: string;
  seenAt: number;
};

type AuthIssueStore = {
  issue: AuthIssue | null;
  reportAuthIssue: (issue: Omit<AuthIssue, "seenAt">) => void;
  clearAuthIssue: () => void;
};

const useAuthIssueStore = create<AuthIssueStore>((set) => ({
  issue: null,
  reportAuthIssue: (issue) => {
    set({ issue: { ...issue, seenAt: Date.now() } });
  },
  clearAuthIssue: () => {
    set({ issue: null });
  },
}));

export { useAuthIssueStore };
export type { AuthIssue };
