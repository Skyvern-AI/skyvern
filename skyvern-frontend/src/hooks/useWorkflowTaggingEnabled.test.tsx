// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: getMock }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useRunTagsBatchQuery } from "@/routes/tasks/hooks/useRunTagsBatchQuery";
import { useRunTagsQuery } from "@/routes/tasks/hooks/useRunTagsQuery";
import { useRunTagSuggestionsQuery } from "@/routes/tasks/hooks/useRunTagSuggestionsQuery";
import { useTagKeysQuery } from "@/routes/workflows/hooks/useTagKeysQuery";
import {
  useTagValuesListQuery,
  useTagValuesQuery,
} from "@/routes/workflows/hooks/useTagValuesQuery";
import { useWorkflowTagsBatchQuery } from "@/routes/workflows/hooks/useWorkflowTagsBatchQuery";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";

const TAG_QUERIES: Array<[string, () => { fetchStatus: string }, string]> = [
  ["useTagKeysQuery", () => useTagKeysQuery(), "/tag-keys"],
  ["useTagValuesQuery", () => useTagValuesQuery(), "/tag-values"],
  ["useTagValuesListQuery", () => useTagValuesListQuery(), "/tag-values"],
  [
    "useWorkflowTagsBatchQuery",
    () => useWorkflowTagsBatchQuery(["wpid_1"]),
    "/workflow-tags",
  ],
  ["useRunTagsQuery", () => useRunTagsQuery("wr_1"), "/runs/wr_1/tags"],
  ["useRunTagsBatchQuery", () => useRunTagsBatchQuery(["wr_1"]), "/run-tags"],
  [
    "useRunTagSuggestionsQuery",
    () => useRunTagSuggestionsQuery(),
    "/run-tag-suggestions",
  ],
];

function wrapperFor(tagging: boolean | undefined) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <FeatureFlagContext.Provider
          value={(flag) =>
            flag === WORKFLOW_TAGGING_FLAG ? tagging : undefined
          }
        >
          {children}
        </FeatureFlagContext.Provider>
      </QueryClientProvider>
    );
  };
}

afterEach(() => {
  getMock.mockReset();
});

// Every tag route 403s for an org with the flag off, so each tag query must gate itself rather than rely on callers.
describe("tag queries wait for the tagging flag", () => {
  it.each(
    TAG_QUERIES.flatMap(([name, hook]) => [
      [name, "pending", undefined, hook] as const,
      [name, "off", false, hook] as const,
    ]),
  )("%s sends nothing while the flag is %s", (_name, _state, flag, hook) => {
    const { result } = renderHook(hook, { wrapper: wrapperFor(flag) });

    expect(result.current.fetchStatus).toBe("idle");
    expect(getMock).not.toHaveBeenCalled();
  });

  it.each(TAG_QUERIES)(
    "%s fetches once the flag is on",
    async (_name, hook, path) => {
      getMock.mockResolvedValue({ data: [] });

      renderHook(hook, { wrapper: wrapperFor(true) });

      await waitFor(() =>
        expect(getMock.mock.calls.map(([url]) => url)).toEqual([path]),
      );
    },
  );
});
