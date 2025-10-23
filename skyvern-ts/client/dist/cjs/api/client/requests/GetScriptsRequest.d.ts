/**
 * @example
 *     {
 *         page: 1,
 *         page_size: 10
 *     }
 */
export interface GetScriptsRequest {
    /** Page number for pagination */
    page?: number;
    /** Number of items per page */
    page_size?: number;
}
