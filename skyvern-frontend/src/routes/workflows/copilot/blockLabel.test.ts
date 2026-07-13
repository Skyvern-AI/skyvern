import { describe, expect, it } from "vitest";

import { humanizeBlockLabel } from "./blockLabel";

describe("humanizeBlockLabel", () => {
  it("title-cases underscore-separated words", () => {
    expect(
      humanizeBlockLabel("extract_first_comments_from_top_three_posts"),
    ).toBe("Extract First Comments From Top Three Posts");
  });

  it("strips a trailing retry-count version suffix", () => {
    expect(
      humanizeBlockLabel(
        "hn_visit_first_second_third_comment_pages_extract_top_comments_v2",
      ),
    ).toBe("Hn Visit First Second Third Comment Pages Extract Top Comments");
  });

  it("strips a double-digit version suffix", () => {
    expect(humanizeBlockLabel("log_event_v10")).toBe("Log Event");
  });

  it("does not treat a mid-label _v as a version suffix", () => {
    expect(humanizeBlockLabel("v2_report_summary")).toBe("V2 Report Summary");
  });

  it("leaves an already-uppercase segment intact", () => {
    expect(humanizeBlockLabel("fetch_URL_data")).toBe("Fetch URL Data");
  });

  it("falls back to the raw label when nothing survives stripping", () => {
    expect(humanizeBlockLabel("_v2")).toBe("_v2");
  });

  it("falls back to the raw label for an empty string", () => {
    expect(humanizeBlockLabel("")).toBe("");
  });

  it("handles a single word with no underscores", () => {
    expect(humanizeBlockLabel("block_1")).toBe("Block 1");
  });
});
