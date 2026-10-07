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
          parameters: [{ key: "cred_param", parameter_type: "credential" }],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("finds editor login blocks without credential parameter keys", () => {
    expect(
      getLoginBlocksWithoutCredentials(
        [
          { block_type: "login", label: "block_1", parameter_keys: [] },
          {
            block_type: "login",
            label: "block_2",
            parameter_keys: ["cred_param"],
          },
        ],
        (key) => key === "cred_param",
      ),
    ).toEqual([{ label: "block_1" }]);
  });

  it("accepts a workflow input carrying a credential id", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          parameters: [
            {
              key: "cred",
              parameter_type: "workflow",
              data_type: "credential_id",
            },
          ],
        },
      ]),
    ).toEqual([]);
  });

  it("accepts the persisted workflow_parameter_type spelling the run form sees", () => {
    // The run form validates persisted blocks, where the field is
    // `workflow_parameter_type`; only the editor renames it to `dataType`.
    // Reading just the editor spelling disabled Run on a valid workflow.
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          parameters: [
            {
              key: "cred",
              parameter_type: "workflow",
              workflow_parameter_type: "credential_id",
            },
          ],
        },
      ]),
    ).toEqual([]);
  });

  it("still flags a workflow input that is not a credential id", () => {
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          parameters: [
            {
              key: "plain",
              parameter_type: "workflow",
              workflow_parameter_type: "string",
            },
          ],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("flags a login block whose remaining parameter is not a credential", () => {
    // Removing the credential via Advanced > Parameters can leave an ordinary
    // parameter behind, so a non-empty list does not mean a credential is set.
    expect(
      getLoginBlocksWithoutCredentials([
        {
          block_type: "login",
          label: "block_1",
          parameters: [{ key: "some_output", parameter_type: "output" }],
        },
        {
          block_type: "login",
          label: "block_2",
          parameters: [{ key: "cred", parameter_type: "credential" }],
        },
      ]),
    ).toEqual([{ label: "block_1" }]);
  });

  it("flags an editor login block whose remaining key is not a credential", () => {
    const isCredentialKey = (key: string) => key === "cred";
    expect(
      getLoginBlocksWithoutCredentials(
        [
          {
            block_type: "login",
            label: "block_1",
            parameter_keys: ["leftover"],
          },
          { block_type: "login", label: "block_2", parameter_keys: ["cred"] },
        ],
        isCredentialKey,
      ),
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
