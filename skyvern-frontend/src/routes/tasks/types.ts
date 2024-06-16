export const sampleCases = [
  "blank",
  "geico",
  "finditparts",
  "california_edd",
  "bci_seguros",
  "job_application",
] as const;

export type SampleCase = (typeof sampleCases)[number];
