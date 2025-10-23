/**
 * Output data from the run, if any. Format/schema depends on the data extracted by the run.
 */
export type TaskRunResponseOutput = Record<string, unknown> | unknown[] | string;
