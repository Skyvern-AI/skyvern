from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text


def test_sanitize_postgres_text__with_nul_bytes() -> None:
    """Test that NUL bytes (0x00) are removed."""
    text_with_nul = "Hello\x00World\x00Test"
    expected = "HelloWorldTest"
    result = sanitize_postgres_text(text_with_nul)
    assert result == expected


def test_sanitize_postgres_text__with_control_characters() -> None:
    """Test that problematic control characters are removed."""
    # Test various control characters that should be removed
    text_with_controls = "Hello\x01\x02\x03World\x08\x0b\x0c\x0e\x1fTest"
    expected = "HelloWorldTest"
    result = sanitize_postgres_text(text_with_controls)
    assert result == expected


def test_sanitize_postgres_text__preserve_whitespace() -> None:
    """Test that common whitespace characters are preserved."""
    text_with_whitespace = "Hello\tWorld\nNew Line\rCarriage Return"
    result = sanitize_postgres_text(text_with_whitespace)
    assert result == text_with_whitespace
    assert "\t" in result
    assert "\n" in result
    assert "\r" in result


def test_sanitize_postgres_text__mixed_case() -> None:
    """Test text with mix of normal text, NUL bytes, and control characters."""
    mixed_text = "PDF Text\x00with NUL\tbytes\nand\x01control\x08chars\rand normal text."
    # \r should be preserved as it's a valid whitespace character
    expected = "PDF Textwith NUL\tbytes\nandcontrolchars\rand normal text."
    result = sanitize_postgres_text(mixed_text)
    assert result == expected


def test_sanitize_postgres_text__unicode_text() -> None:
    """Test that Unicode characters are preserved."""
    unicode_text = "中文测试 Unicode: café, naïve, Ω, emoji 😀"
    result = sanitize_postgres_text(unicode_text)
    assert result == unicode_text


def test_sanitize_postgres_text__surrogates() -> None:
    result = sanitize_postgres_text("high:\ud800 low:\udc00 pair:\ud83d\ude00 emoji:😀\x00")
    assert result == "high:\ufffd low:\ufffd pair:😀 emoji:😀"
    assert result.encode("utf-8")
