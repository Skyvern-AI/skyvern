import type * as Skyvern from "../../index.mjs";
/**
 * @example
 *     {}
 */
export interface CreateScriptRequest {
    /** Associated workflow ID */
    workflow_id?: string;
    /** Associated run ID */
    run_id?: string;
    /** Array of files to include in the script */
    files?: Skyvern.ScriptFileCreate[];
}
