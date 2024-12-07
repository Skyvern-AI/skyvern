import { Status } from "@/api/types";

export const sampleCases = [
  "blank",
  "geico",
  "finditparts",
  "california_edd",
  "bci_seguros",
  "job_application",
  "contact_us_forms",
  "hackernews",
  "AAPLStockPrice",
  "NYTBestseller",
  "topRankedFootballTeam",
  "extractIntegrationsFromGong",
] as const;

export type SampleCase = (typeof sampleCases)[number];

export function statusIsNotFinalized({ status }: { status: Status }): boolean {
  return (
    status === Status.Created ||
    status === Status.Queued ||
    status === Status.Running
  );
}

export function statusIsFinalized({ status }: { status: Status }): boolean {
  return (
    status === Status.Completed ||
    status === Status.Failed ||
    status === Status.Terminated ||
    status === Status.TimedOut ||
    status === Status.Canceled
  );
}

export function statusIsRunningOrQueued({
  status,
}: {
  status: Status;
}): boolean {
  return status === Status.Queued || status === Status.Running;
}
