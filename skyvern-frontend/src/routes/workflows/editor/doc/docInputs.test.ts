import { describe, expect, test } from "vitest";
import type { Node } from "@xyflow/react";
import { WorkflowParameterValueType } from "../../types/workflowTypes";
import type { ParametersState } from "../types";
import {
  docInputNameProblem,
  parseDocInputForm,
  removeDocInput,
  saveDocInput,
} from "./docInputs";

describe("doc input form", () => {
  test("rejects a name reserved for a step output", () => {
    const saved = parseDocInputForm(
      {
        key: "lookup_output",
        dataType: WorkflowParameterValueType.String,
        defaultText: "",
        description: "",
      },
      "query",
      [] as unknown as ParametersState,
      [{ data: { label: "lookup" } }] as unknown as Node[],
    );
    expect(saved).toEqual({
      error: "Another input or step result already has this name.",
    });
  });

  test("a saved empty-string default stays when only the description changes", () => {
    const parameters = [
      {
        key: "note",
        parameterType: "workflow",
        dataType: WorkflowParameterValueType.String,
        description: null,
        defaultValue: "",
      },
    ] as unknown as ParametersState;
    const saved = parseDocInputForm(
      {
        key: "note",
        dataType: WorkflowParameterValueType.String,
        defaultText: "",
        description: "Shown to the person running it",
      },
      "note",
      parameters,
      [],
    );
    expect(saved).toMatchObject({
      parameter: {
        defaultValue: "",
        description: "Shown to the person running it",
      },
    });
    const fresh = parseDocInputForm(
      {
        key: "other",
        dataType: WorkflowParameterValueType.String,
        defaultText: "",
        description: "",
      },
      null,
      parameters,
      [],
    );
    expect(fresh).toMatchObject({ parameter: { defaultValue: null } });
  });

  test("deleting an input clears loop sources derived from it", () => {
    const parameters = [
      {
        key: "customer",
        parameterType: "workflow",
        dataType: WorkflowParameterValueType.JSON,
        description: null,
        defaultValue: null,
      },
    ] as unknown as ParametersState;
    const loop = (id: string, ref: string, value = "") =>
      ({
        id,
        type: "loop",
        position: { x: 0, y: 0 },
        data: { label: id, loopValue: value, loopVariableReference: ref },
      }) as never;
    const result = removeDocInput(parameters, "customer");
    if ("refused" in result) throw new Error(result.refused);
    const nodes = result.nodes([
      loop("a", "customer.items"),
      loop("b", 'customer["items"]'),
      loop("c", "{{ customer.items }}"),
      loop("d", "other.items"),
      loop("e", "{{ customer }}"),
    ]) as Array<{ data: { loopVariableReference: string } }>;
    expect(nodes.map((n) => n.data.loopVariableReference)).toEqual([
      "",
      "",
      "",
      "other.items",
      "",
    ]);
  });

  test("renaming an input follows parameterKeys on every step type that carries them", () => {
    const parameters = [
      {
        key: "rows",
        parameterType: "workflow",
        dataType: WorkflowParameterValueType.String,
        description: null,
        defaultValue: null,
      },
    ] as unknown as ParametersState;
    const types = [
      "pdfParser",
      "emailInbox",
      "workflowTrigger",
      "googleSheetsRead",
      "taskv2",
      "sendEmail",
    ];
    const step = (type: string) =>
      ({
        id: type,
        type,
        position: { x: 0, y: 0 },
        data: { label: type, parameterKeys: ["rows", "other"] },
      }) as never;
    const { nodes } = saveDocInput(parameters, "rows", {
      ...(parameters[0] as object),
      key: "lines",
    } as never);
    const renamed = nodes!(types.map(step)) as Array<{
      data: { parameterKeys: string[] };
    }>;
    expect(renamed.map((n) => n.data.parameterKeys)).toEqual(
      types.map(() => ["lines", "other"]),
    );
  });
});

describe("naming an input", () => {
  test("a reserved name is refused when a new input is renamed to it, but an existing one keeps its own", () => {
    expect(docInputNameProblem("current_date", "new_input", [], [])).toMatch(
      /reserved/,
    );
    expect(docInputNameProblem("current_date", null, [], [])).toMatch(
      /reserved/,
    );
    expect(docInputNameProblem("current_date", "current_date", [], [])).toBe(
      null,
    );
    expect(
      docInputNameProblem("start_url", "new_input", [], ["start_url"]),
    ).toMatch(/already has this name/);
    expect(docInputNameProblem("start_url", "new_input", [], [])).toBe(null);
  });
});
