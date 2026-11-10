import { describe, expect, test } from "vitest";

import { WorkflowParameterValueType } from "../../types/workflowTypes";
import type { ParametersState } from "../types";
import { getDocInputChips, NOT_USED_TITLE } from "./docInputChips";

describe("getDocInputChips", () => {
  test("an unused input's value title still says it is unused", () => {
    const url = "https://example.com/uploads/2026/passport.jpg";
    const [chip] = getDocInputChips(
      [
        {
          key: "passport",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: url,
        },
      ] as ParametersState,
      [],
    );
    expect(chip?.unused).toBe(true);
    expect(chip?.valueTitle).toBe(`${url}\n${NOT_USED_TITLE}`);
  });
});
