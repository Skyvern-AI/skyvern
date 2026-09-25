_PAGE_CHANGE_MESSAGES = (
    "Execution context was destroyed",
    "The page changed while the extension operation was running.",
    "The page changed before the extension operation started.",
)


def is_page_change_error(error: Exception | str) -> bool:
    return any(message in str(error) for message in _PAGE_CHANGE_MESSAGES)
