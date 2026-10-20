// @vitest-environment jsdom

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { Status } from "@/api/types";
import { useToast } from "@/components/ui/use-toast";
import { claimRunCompletionNotice } from "./runCompletionNotices";
import { useRunCompletionToast } from "./useRunCompletionToast";

afterEach(cleanup);

test("block-specific completion wins across observers and never toasts twice", () => {
  const run = {
    workflow_run_id: "wr_block_notice",
    status: Status.Running,
    retry_pending: false,
    failure_reason: "Step failed",
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={["/studio?wr=wr_block_notice&bl=step_1"]}>
      {children}
    </MemoryRouter>
  );
  const { result, rerender } = renderHook(
    ({ status, retry_pending }) => {
      const state = useToast();
      useRunCompletionToast({ ...run, status, retry_pending });
      useRunCompletionToast({ ...run, status, retry_pending });
      return state.toasts;
    },
    {
      wrapper,
      initialProps: { status: Status.Running as Status, retry_pending: false },
    },
  );
  const previousId = result.current[0]?.id;
  rerender({ status: Status.Failed, retry_pending: true });
  expect(result.current[0]?.id).toBe(previousId);
  rerender({ status: Status.Failed, retry_pending: false });
  expect(result.current[0]?.title).toBe("Agent Block step_1: failed");
  expect(result.current[0]?.description).toBe("Reason: Step failed");
  const noticeId = result.current[0]?.id;
  rerender({ status: Status.Failed, retry_pending: false });
  expect(result.current[0]?.id).toBe(noticeId);
  expect(
    claimRunCompletionNotice(run.workflow_run_id, () => {
      throw new Error("Duplicate notice");
    }),
  ).toBe(false);
});

test("a failed display does not consume the run's notice", () => {
  expect(() =>
    claimRunCompletionNotice("wr_notice_failure", () => {
      throw new Error("Display failed");
    }),
  ).toThrow("Display failed");
  const displayed: string[] = [];
  expect(
    claimRunCompletionNotice("wr_notice_failure", () => {
      displayed.push("shown");
    }),
  ).toBe(true);
  expect(displayed).toEqual(["shown"]);
});
