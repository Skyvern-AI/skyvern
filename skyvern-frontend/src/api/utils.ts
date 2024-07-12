import { Status, TaskApiResponse } from "./types";

const finalTaskStates: Array<Status> = [
  Status.Canceled,
  Status.Completed,
  Status.Terminated,
  Status.TimedOut,
  Status.Failed,
];

export function taskIsFinalized(task: TaskApiResponse) {
  return finalTaskStates.includes(task.status);
}
