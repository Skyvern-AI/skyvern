from skyvern.forge.sdk.db.utils import escape_like_term, summarize_copilot_chat_title


def test_short_content_is_returned_verbatim() -> None:
    assert summarize_copilot_chat_title("Build a login flow") == "Build a login flow"


def test_whitespace_and_newlines_collapse_to_one_line() -> None:
    assert summarize_copilot_chat_title("Build   a\nlogin\t flow\n\n") == "Build a login flow"


def test_blank_content_becomes_empty_title() -> None:
    assert summarize_copilot_chat_title("   \n\t ") == ""


def test_long_content_is_truncated_with_ellipsis() -> None:
    content = "a" * 500
    title = summarize_copilot_chat_title(content, max_length=120)
    assert len(title) == 120
    assert title.endswith("…")


def test_truncation_respects_custom_max_length() -> None:
    title = summarize_copilot_chat_title("word " * 50, max_length=20)
    assert len(title) <= 20
    assert title.endswith("…")


def test_escape_like_term_escapes_metacharacters() -> None:
    assert escape_like_term("100%") == "100\\%"
    assert escape_like_term("a_b") == "a\\_b"
    assert escape_like_term("c\\d") == "c\\\\d"


def test_escape_like_term_escapes_backslash_before_wildcards() -> None:
    assert escape_like_term("\\%_") == "\\\\\\%\\_"


def test_escape_like_term_leaves_plain_text_untouched() -> None:
    assert escape_like_term("deploy login flow") == "deploy login flow"
