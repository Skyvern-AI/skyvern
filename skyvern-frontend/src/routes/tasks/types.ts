export const sampleCases = [
  "blank",
  "geico",
  "finditparts",
  "california_edd",
  "bci_seguros",
] as const;

export type SampleCase = (typeof sampleCases)[number];
