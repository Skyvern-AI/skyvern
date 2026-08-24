import { describe, expect, it } from "vitest";

import {
  COPILOT_WORKING_VERBS,
  pickWorkingVerb,
  VERB_CYCLE_MS,
} from "./workingVerbs";

it("cycles working verbs every six seconds", () => {
  expect(VERB_CYCLE_MS).toBe(6000);
});

describe("pickWorkingVerb", () => {
  it("never repeats the verb it just showed", () => {
    let previous = pickWorkingVerb();
    for (let i = 0; i < 500; i += 1) {
      const next = pickWorkingVerb(previous);
      expect(next).not.toBe(previous);
      expect(COPILOT_WORKING_VERBS).toContain(next);
      previous = next;
    }
  });
});
