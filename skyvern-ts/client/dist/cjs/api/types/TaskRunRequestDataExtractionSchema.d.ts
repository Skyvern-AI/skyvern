/**
 *
 * The schema for data to be extracted from the webpage. If you're looking for consistent data schema being returned by the agent, it's highly recommended to use https://json-schema.org/.
 */
export type TaskRunRequestDataExtractionSchema = Record<string, unknown> | unknown[] | string;
