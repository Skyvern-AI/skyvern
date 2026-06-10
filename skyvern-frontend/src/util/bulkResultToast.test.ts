import { beforeEach, describe, expect, it, vi } from "vitest";

import { bulkResultToast } from "./bulkResultToast";
import { toast } from "@/components/ui/use-toast";

vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
}));

const titles = {
  successTitle: (count: number) => `Cloned ${count}.`,
  failureTitle: (count: number) => `Failed ${count}.`,
  partialTitle: (succeeded: number, failed: number) =>
    `Cloned ${succeeded}. ${failed} failed.`,
};

function rejected(message: string): PromiseSettledResult<unknown> {
  return { status: "rejected", reason: new Error(message) };
}

const fulfilled: PromiseSettledResult<unknown> = {
  status: "fulfilled",
  value: undefined,
};

describe("bulkResultToast", () => {
  beforeEach(() => {
    vi.mocked(toast).mockClear();
  });

  it("uses success variant when everything succeeded", () => {
    bulkResultToast({ succeeded: 3, total: 3, ...titles });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Cloned 3.", variant: "success" }),
    );
  });

  it("uses destructive variant when everything failed", () => {
    bulkResultToast({ succeeded: 0, total: 2, ...titles });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Failed 2.", variant: "destructive" }),
    );
  });

  it("uses warning variant for partial success", () => {
    bulkResultToast({ succeeded: 2, total: 3, ...titles });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Cloned 2. 1 failed.",
        variant: "warning",
      }),
    );
  });

  it("surfaces the first rejection message as the description", () => {
    bulkResultToast({
      succeeded: 1,
      total: 3,
      results: [fulfilled, rejected("403 Forbidden"), rejected("timeout")],
      ...titles,
    });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "403 Forbidden" }),
    );
  });

  it("keeps the warning variant on partial success without results", () => {
    bulkResultToast({ succeeded: 1, total: 3, ...titles });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "warning", description: undefined }),
    );
  });

  it("prefers the API error detail over the generic axios message", () => {
    const axiosLike: PromiseSettledResult<unknown> = {
      status: "rejected",
      reason: Object.assign(new Error("Request failed with status code 403"), {
        response: { data: { detail: "Folder is read-only" } },
      }),
    };
    bulkResultToast({
      succeeded: 0,
      total: 1,
      results: [axiosLike],
      ...titles,
    });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "Folder is read-only" }),
    );
  });

  it("omits the description when nothing failed", () => {
    bulkResultToast({
      succeeded: 1,
      total: 1,
      results: [fulfilled],
      ...titles,
    });

    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ description: undefined }),
    );
  });
});
