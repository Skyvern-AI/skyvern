export declare const RunStatus: {
    readonly Created: "created";
    readonly Queued: "queued";
    readonly Running: "running";
    readonly TimedOut: "timed_out";
    readonly Failed: "failed";
    readonly Terminated: "terminated";
    readonly Completed: "completed";
    readonly Canceled: "canceled";
};
export type RunStatus = (typeof RunStatus)[keyof typeof RunStatus];
