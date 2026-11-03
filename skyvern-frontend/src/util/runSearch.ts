// Mirrors the search_key field set each runs endpoint matches on; shown as a tooltip because the
// search input is too narrow to spell it out in the placeholder. Identifiers match by equality,
// so a partial ID finds nothing; a complete profile or session ID is not searched as text.
const RUN_IDENTIFIER_FIELDS_HINT =
  "Paste a complete browser profile, browser session or credential ID to find the runs that used it.";

// /v1/runs: the run history table. Standalone tasks carry only a browser session ID.
const RUN_HISTORY_SEARCH_FIELDS_HINT = `Searches run ID, agent ID, title, URL, inputs and a workflow run's webhook callback URL. ${RUN_IDENTIFIER_FIELDS_HINT}`;

// /workflows/{id}/runs: a title match returns every run of that workflow; URL is not searched.
const WORKFLOW_RUN_SEARCH_FIELDS_HINT = `Searches run ID, agent ID, title, inputs and webhook callback URL. ${RUN_IDENTIFIER_FIELDS_HINT}`;

export { RUN_HISTORY_SEARCH_FIELDS_HINT, WORKFLOW_RUN_SEARCH_FIELDS_HINT };
