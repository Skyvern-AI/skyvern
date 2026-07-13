import type { BranchCondition } from "../../../types/workflowTypes";

function getExcelStyleLetter(index: number): string {
  let result = "";
  let num = index;

  while (num >= 0) {
    result = String.fromCharCode(65 + (num % 26)) + result;
    num = Math.floor(num / 26) - 1;
  }

  return result;
}

export function getConditionLabel(
  branch: BranchCondition,
  index: number,
): string {
  const letter = getExcelStyleLetter(index);
  if (branch.is_default) return `${letter} • Else`;
  if (index === 0) return `${letter} • If`;
  return `${letter} • Else If`;
}

export function orderBranchesWithDefaultsLast(
  branches: Array<BranchCondition>,
): Array<BranchCondition> {
  const nonDefaultBranches = branches.filter((branch) => !branch.is_default);
  // Keep every default branch visible so invalid or imported data remains editable.
  const defaultBranches = branches.filter((branch) => branch.is_default);
  return [...nonDefaultBranches, ...defaultBranches];
}
