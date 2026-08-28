import { expect, it } from "vitest";
import { RunEngine } from "@/api/types";
import * as example from "./onboardingExample";
const sensitiveKey =
  /credential|session|profile|header|webhook|totp|(^|_)id($|_)/i;
function assertNoSensitiveKeys(value: unknown): void {
  if (Array.isArray(value)) return value.forEach(assertNoSensitiveKeys);
  if (typeof value !== "object" || value === null) return;
  Object.entries(value).forEach(([key, nested]) => {
    expect(key).not.toMatch(sensitiveKey);
    assertNoSensitiveKeys(nested);
  });
}
it("keeps the copy payload to one passive public task", () => {
  const { blocks } = example.onboardingExampleRequest.workflow_definition;
  expect(blocks).toHaveLength(1);
  const task = blocks[0]!;
  expect(task).toMatchObject({
    block_type: "task",
    url: "https://www.skyvern.com/",
    navigation_goal: null,
  });
  expect(task.engine).toBe(RunEngine.SkyvernV1);
  assertNoSensitiveKeys(example.onboardingExampleRequest);
});
it("labels playback and result data as synthetic examples", () => {
  const { provenance, playback, result } =
    example.onboardingExamplePresentation;
  expect(provenance).toBe("Example data, not your run");
  expect(playback[0]).toMatch(/static example/);
  expect(playback[2]).toMatch(/example result/);
  expect(result.title).toMatch(/synthetic.*example/i);
  expect(result.fields.map(({ label }) => label)).toEqual([
    "Headline",
    "Product summary",
  ]);
});
