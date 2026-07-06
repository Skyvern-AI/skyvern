import { describe, expect, it } from "vitest";

import { getLoginBlocksWithoutCredentials } from "./runValidation";

describe("getLoginBlocksWithoutCredentials", () => {
  it("finds persisted login blocks without credential parameters", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        { block_type: "login", label: "block_1", parameters: [] },
        {
          block_type: "login",
          label: "block_2",
          parameters: [{ key: "cred_param" }],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("finds editor login blocks without credential parameter keys", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        { block_type: "login", label: "block_1", parameter_keys: [] },
        {
          block_type: "login",
          label: "block_2",
          parameter_keys: ["cred_param"],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("walks nested loop blocks", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "for_loop",
          label: "loop_1",
          loop_blocks: [
            { block_type: "task", label: "block_1" },
            { block_type: "login", label: "block_2", parameters: [] },
          ],
        },
      ]),
    ).toEqual([{ label: "block_2" }]);
  });
});
