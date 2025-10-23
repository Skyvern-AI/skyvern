/**
 * @example
 *     {
 *         page: 1,
 *         page_size: 1,
 *         only_saved_tasks: true,
 *         only_workflows: true,
 *         search_key: "search_key",
 *         title: "title",
 *         template: true
 *     }
 */
export interface GetWorkflowsRequest {
    page?: number;
    page_size?: number;
    only_saved_tasks?: boolean;
    only_workflows?: boolean;
    /** Unified search across workflow title and parameter metadata (key, description, default_value). */
    search_key?: string;
    /** Deprecated: use search_key instead. */
    title?: string;
    template?: boolean;
}
