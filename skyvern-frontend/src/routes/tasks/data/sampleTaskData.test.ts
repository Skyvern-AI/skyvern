import { describe, expect, it } from "vitest";
import { createNewTaskFormSchema } from "../create/taskFormTypes";
import { sampleCases } from "../types";
import { getSampleForInitialFormValues } from "./sampleTaskData";

describe("getSampleForInitialFormValues", () => {
  it.each(sampleCases)(
    "produces values the create-task resolver accepts for %s",
    (sample) => {
      const values = getSampleForInitialFormValues(sample);
      // The blank sample intentionally ships empty goals, which the form expects
      // the user to fill in before submitting.
      const result = createNewTaskFormSchema.safeParse({
        ...values,
        navigationGoal: values.navigationGoal || "do something",
      });

      expect(result.error?.issues ?? []).toEqual([]);
    },
  );
});
