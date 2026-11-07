import type { GetRunResponse } from "../api/index.js";
import { RunStatus } from "../api/index.js";

const RUN_STATUS_IS_FINAL: Record<RunStatus, boolean> = {
    [RunStatus.Created]: false,
    [RunStatus.Queued]: false,
    [RunStatus.Running]: false,
    [RunStatus.Paused]: false,
    [RunStatus.TimedOut]: true,
    [RunStatus.Failed]: true,
    [RunStatus.Terminated]: true,
    [RunStatus.Completed]: true,
    [RunStatus.Canceled]: true,
};

/**
 * Whether the run has reached a status the server will not move it off of.
 *
 * A workflow run under a retry policy reports a final status with `retry_pending` set while the
 * server still has another attempt to run, so that flag has to clear too. `retry_pending` is not
 * on the generated response types yet, hence the structural read.
 */
export function isRunSettled(run: GetRunResponse): boolean {
    if (!RUN_STATUS_IS_FINAL[run.status]) {
        return false;
    }
    return (run as { retry_pending?: boolean }).retry_pending !== true;
}
