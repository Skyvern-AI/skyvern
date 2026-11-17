AGENT_STEP_TOOL_NAMES = frozenset(
    {
        "browser_task",
        "code",
        "data_export",
        "download_to_s3",
        "email_inbox",
        "file_download",
        "file_upload",
        "file_url_parser",
        "finish",
        "http_request",
        "human_interaction",
        "load_skill",
        "login",
        "page_click",
        "page_extract",
        "page_fill",
        "page_goto",
        "pdf_fill",
        "print_page",
        "read_run_data",
        "send_email",
        "split_pdf",
        "text_prompt",
        "upload_to_s3",
        "wait",
        "web_search",
    }
)
# Offered whatever the step's list says: neither changes anything outside the step.
ALWAYS_OFFERED_TOOLS = frozenset({"finish", "read_run_data"})
LEGACY_AGENT_STEP_TOOL_NAMES = {
    "pdf_parser": "file_url_parser",
    "google_sheets_read": "google_sheets__read_rows",
    "google_sheets_write": "google_sheets__write_rows",
}
# The built-in catalog actions a step may list, as "<integration id>__<action id>"; a names-only copy so this module
# need not import the catalog. agent_step_tools.py raises at import if it differs from the catalog.
CATALOG_TOOL_NAMES = frozenset(
    {
        "gmail__read_inbox",
        "gmail__send_email",
        "google_drive__upload_files",
        "google_sheets__read_rows",
        "google_sheets__write_rows",
        "outlook__read_inbox",
        "powerbi__query",
    }
)


def catalog_tool_name(integration_id: str, action_id: str) -> str:
    return f"{integration_id}__{action_id}"


def is_known_tool_name(name: str) -> bool:
    return name in AGENT_STEP_TOOL_NAMES or name in CATALOG_TOOL_NAMES


def current_tool_names(names: list[str]) -> list[str]:
    return list(dict.fromkeys(LEGACY_AGENT_STEP_TOOL_NAMES.get(name, name) for name in names))
