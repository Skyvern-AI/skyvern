import { describe, expect, it, vi } from "vitest";
import {
  markCommit,
  markLoad,
  markMessage,
  resetStreamStats,
  snapshot,
} from "./streamStats";

describe("streamStats", () => {
  it("reports p50/p90 of message->commit->load per frame", () => {
    resetStreamStats();
    const now = vi.spyOn(performance, "now");
    let t = 0;
    now.mockImplementation(() => t);
    for (const [commitDelay, loadDelay] of [
      [5, 10],
      [7, 12],
      [50, 60],
    ] as const) {
      t += 100;
      const token = markMessage();
      t += commitDelay;
      markCommit(token);
      t += loadDelay;
      markLoad(token);
    }
    const s = snapshot();
    expect(s.frames).toBe(3);
    expect(s.parseToCommitMs.p50).toBe(7);
    expect(s.commitToLoadMs.p50).toBe(12);
    expect(s.messageToLoadMs.p90).toBe(110);
    now.mockRestore();
  });
});
