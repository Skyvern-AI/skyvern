import type * as Skyvern from "../../index.mjs";
/**
 * @example
 *     {
 *         files: [{
 *                 path: "src/main.py",
 *                 content: "content"
 *             }]
 *     }
 */
export interface DeployScriptRequest {
    /** Array of files to include in the script */
    files: Skyvern.ScriptFileCreate[];
}
