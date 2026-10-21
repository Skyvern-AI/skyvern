import { describe, expect, it } from "vitest";

import { getLoginBlocksWithoutCredentials } from "./runValidation";

describe("getLoginBlocksWithoutCredentials", () => {
  it("flags persisted login blocks with no non-URL parameters", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        { block_type: "login", label: "block_1", parameters: [] },
        {
          block_type: "login",
          label: "block_2",
          parameters: [{ key: "cred_param", parameter_type: "credential" }],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("accepts bound non-URL workflow parameters", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          url: "https://example.com/login",
          parameters: [
            {
              key: "username",
              parameter_type: "workflow",
              workflow_parameter_type: "string",
            },
            {
              key: "password",
              parameter_type: "workflow",
              workflow_parameter_type: "string",
            },
          ],
        },
      ]),
    ).toEqual([]);
  });

  it("does not count a URL parameter as a non-URL binding", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          url: "login_url",
          parameters: [
            {
              key: "login_url",
              parameter_type: "workflow",
              workflow_parameter_type: "string",
            },
          ],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("flags an unbound editor login but accepts a plain workflow parameter", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        { block_type: "login", label: "block_1", parameter_keys: [] },
        { block_type: "login", label: "block_2", parameter_keys: ["username"] },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("accepts a bound non-URL block parameter", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          parameters: [{ key: "some_output", parameter_type: "output" }],
        },
      ]),
    ).toEqual([]);
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
