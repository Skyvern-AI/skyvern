export declare const ActionStatus: {
    readonly Pending: "pending";
    readonly Skipped: "skipped";
    readonly Failed: "failed";
    readonly Completed: "completed";
};
export type ActionStatus = (typeof ActionStatus)[keyof typeof ActionStatus];
