import { describe, expect, test } from "vitest";
import type { AppNode } from "../nodes";
import { loginNodeDefaultData } from "../nodes/LoginNode/types";
import { docProblemEntries } from "./docProblems";

describe("login input problems", () => {
  test("a server problem about additional inputs opens Details even when the credential is shown", () => {
    const node = {
      id: "login",
      type: "login",
      position: { x: 0, y: 0 },
      data: {
        ...loginNodeDefaultData,
        label: "login",
        parameterKeys: ["credential", "missing_input"],
      },
    } as AppNode;
    const entries = docProblemEntries({
      nodes: [node],
      items: [],
      fixes: new Map(),
      problems: [
        {
          block_label: "login",
          field: "parameter_keys",
          code: "unknown_parameter",
          message: "Unknown input",
        },
      ],
      editable: true,
      metaKeys: () => ["parameter_keys"],
    });
    expect(entries[0]?.inDetails).toBe(true);
  });
});
