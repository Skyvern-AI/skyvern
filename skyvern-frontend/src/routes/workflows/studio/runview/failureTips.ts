const FAILURE_TIPS: Array<{ match: (reason: string) => boolean; tip: string }> =
  [
    {
      match: (reason) => reason.includes("Invalid master password"),
      tip: "Tip: If inputting the master password via Docker Compose or in any container environment, make sure to double any dollar signs and do not surround it with quotes.",
    },
  ];

export function matchFailureTips(reason: string | null): string[] {
  if (!reason) {
    return [];
  }
  return FAILURE_TIPS.filter(({ match }) => match(reason)).map(
    ({ tip }) => tip,
  );
}
