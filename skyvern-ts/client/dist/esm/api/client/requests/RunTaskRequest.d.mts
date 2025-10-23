import type * as Skyvern from "../../index.mjs";
/**
 * @example
 *     {
 *         "x-user-agent": "x-user-agent",
 *         body: {
 *             prompt: "Find the top 3 posts on Hacker News."
 *         }
 *     }
 */
export interface RunTaskRequest {
    "x-user-agent"?: string;
    body: Skyvern.TaskRunRequest;
}
