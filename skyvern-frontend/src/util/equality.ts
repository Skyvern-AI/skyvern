function deepEqualStringArrays(
  a: string[] | null | undefined,
  b: string[] | null | undefined,
): boolean {
  if (a === undefined && b === undefined) return true;
  if (a === undefined || b === undefined) return false;
  if (a === null && b === null) return true;
  if (a === null || b === null) return false;
  if (a.length !== b.length) return false;
  return a.every((val, i) => val === b[i]);
}

export { deepEqualStringArrays };
