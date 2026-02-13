from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text


def test_sanitize_postgres_text__normal_text() -> None:
    """Test that normal text passes through unchanged."""
    normal_text = "Hello, World! This is a normal PDF text with numbers 123 and symbols @#$%."
    result = sanitize_postgres_text(normal_text)
    assert result == normal_text


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


def test_sanitize_postgres_text__empty_string() -> None:
    """Test that empty string is handled correctly."""
    result = sanitize_postgres_text("")
    assert result == ""


def test_sanitize_postgres_text__mixed_case() -> None:
    """Test text with mix of normal text, NUL bytes, and control characters."""
    mixed_text = "PDF Text\x00with NUL\tbytes\nand\x01control\x08chars\rand normal text."
    # \r should be preserved as it's a valid whitespace character
    expected = "PDF Textwith NUL\tbytes\nandcontrolchars\rand normal text."
    result = sanitize_postgres_text(mixed_text)
    assert result == expected


def test_sanitize_postgres_text__multiple_nul_bytes() -> None:
    """Test that multiple consecutive NUL bytes are all removed."""
    text_with_multiple_nuls = "Start\x00\x00\x00Middle\x00\x00End"
    expected = "StartMiddleEnd"
    result = sanitize_postgres_text(text_with_multiple_nuls)
    assert result == expected


def test_sanitize_postgres_text__unicode_text() -> None:
    """Test that Unicode characters are preserved."""
    unicode_text = "中文测试 Unicode: café, naïve, Ω, emoji 😀"
    result = sanitize_postgres_text(unicode_text)
    assert result == unicode_text


def test_sanitize_postgres_text__real_world_pdf_scenario() -> None:
    """Test a realistic scenario with PDF extraction artifacts."""
    # Simulate what might come from a PDF extraction
    pdf_text = "Invoice\x00Number:\t12345\nDate:\t2024-01-01\x00\nTotal:\t$100.00\x01\x02"
    expected = "InvoiceNumber:\t12345\nDate:\t2024-01-01\nTotal:\t$100.00"
    result = sanitize_postgres_text(pdf_text)
    assert result == expected


def test_sanitize_postgres_text__only_control_characters() -> None:
    """Test string with only problematic characters."""
    only_controls = "\x00\x01\x02\x03\x08"
    expected = ""
    result = sanitize_postgres_text(only_controls)
    assert result == expected


def test_sanitize_postgres_text__preserves_spaces_and_punctuation() -> None:
    """Test that normal spaces and punctuation are preserved."""
    text = "Hello, World! How are you? I'm fine. Test@example.com"
    result = sanitize_postgres_text(text)
    assert result == text


def test_sanitize_postgres_text__newlines_and_paragraphs() -> None:
    """Test multi-paragraph text with newlines."""
    multiline_text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    result = sanitize_postgres_text(multiline_text)
    assert result == multiline_text
    assert result.count("\n") == 4
