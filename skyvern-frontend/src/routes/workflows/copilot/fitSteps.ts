export const STEP_SEPARATOR = " → ";

// Returns [head, tail]: the first step plus as many following steps as fit in
// maxWidth, and an ellipsis bridge to the last step ("" when everything fits).
// The caller lets head truncate before tail so the last step stays visible.
export function fitSteps(
  steps: string[],
  maxWidth: number,
  measure: (text: string) => number,
): [string, string] {
  const all = steps.join(STEP_SEPARATOR);
  if (steps.length <= 1 || measure(all) <= maxWidth) return [all, ""];
  const tail =
    steps.length === 2
      ? `${STEP_SEPARATOR}${steps[1]!}`
      : `${STEP_SEPARATOR}…${STEP_SEPARATOR}${steps[steps.length - 1]!}`;
  let head = steps[0]!;
  for (const step of steps.slice(1, -1)) {
    const next = head + STEP_SEPARATOR + step;
    if (measure(next + tail) > maxWidth) break;
    head = next;
  }
  return [head, tail];
}
