"""
Tests for methods in utils.py that use commentjson.loads
"""

from unittest.mock import Mock

import litellm
import pytest

from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.api.llm.utils import (
    parse_api_response,
)


class TestParseApiResponse:
    """Tests for parse_api_response function"""

    def _create_mock_response(self, content: str | None) -> Mock:
        """Helper method to create a mock LiteLLM response with the given content"""
        response = Mock(spec=litellm.ModelResponse)
        response.choices = [Mock()]
        response.choices[0].message = Mock()
        response.choices[0].message.content = content
        return response

    def test_parse_api_response_valid_json(self) -> None:
        """Test parsing a valid JSON response"""
        response = self._create_mock_response('{"action": "click", "element": "button"}')
        result = parse_api_response(response)
        assert result == {"action": "click", "element": "button"}

    def test_parse_api_response_with_assistant_prefix(self) -> None:
        """Test parsing response with assistant prefix"""
        response = self._create_mock_response('"test": "value"}')
        result = parse_api_response(response, add_assistant_prefix=True)
        assert result == {"test": "value"}

    def test_parse_api_response_json_with_comments(self) -> None:
        """Test parsing JSON with comments using commentjson"""
        response = self._create_mock_response("""
        {
            // This is a comment
            "action": "type",
            "text": "hello world" // Another comment
        }
        """)
        result = parse_api_response(response)
        assert result == {"action": "type", "text": "hello world"}

    def test_parse_api_response_markdown_wrapped_json(self) -> None:
        """Test parsing JSON wrapped in markdown code blocks"""
        response = self._create_mock_response("""
        ```json
        {
            "status": "complete",
            "data": ["item1", "item2"]
        }
        ```
        """)
        result = parse_api_response(response)
        assert result == {"status": "complete", "data": ["item1", "item2"]}

    def test_parse_api_response_empty_content(self) -> None:
        """Test handling empty response content"""
        response = self._create_mock_response(None)
        with pytest.raises(InvalidLLMResponseFormat):
            parse_api_response(response)

    def test_parse_api_response_invalid_json_with_auto_fix(self) -> None:
        """Test auto-fixing invalid JSON with unescaped quotes"""
        response = self._create_mock_response('{"message": "This is a "quoted" word"}')
        result = parse_api_response(response)
        assert result == {"message": 'This is a "quoted" word'}

    def test_parse_api_response_completely_invalid_json(self) -> None:
        """Test handling completely invalid JSON that can't be fixed"""
        response = self._create_mock_response("not json at all { incomplete")
        result = parse_api_response(response)
        assert result == {}

    def test_parse_api_response_nested_array_json(self) -> None:
        """Test parsing JSON with nested arrays"""
        response = self._create_mock_response("""
        {
            "actions": [
                {"type": "click", "element": "button1"},
                {"type": "submit"}
            ]
        }
        """)
        result = parse_api_response(response)
        assert result == {"actions": [{"type": "click", "element": "button1"}, {"type": "submit"}]}

    def test_parse_api_response_simple_key_value_json(self) -> None:
        """Test parsing simple key-value JSON"""
        response = self._create_mock_response('{"name": "test", "value": 123}')
        result = parse_api_response(response)
        assert result == {"name": "test", "value": 123}

    def test_parse_api_response_cutoff_json_simple(self) -> None:
        """Test parsing JSON that's cut off in the middle"""
        response = self._create_mock_response("""
                {
                    "actions": [
                        {"type": "click", "ele
                """)
        result = parse_api_response(response)
        # Should fix the cutoff JSON by completing the incomplete structure
        assert "actions" in result
        assert isinstance(result["actions"], list)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "click"

    def test_parse_api_response_cutoff_json_complex(self) -> None:
        """Test parsing complex JSON that's cut off"""
        response = self._create_mock_response("""
        {
            "actions": [
                {"type": "click", "element": "button1"},
                {"type": "type", "text": "hello"},
                {"type": "click", "element": "butt
        """)
        result = parse_api_response(response)
        # Should fix the cutoff JSON by completing the incomplete structure
        assert "actions" in result
        assert isinstance(result["actions"], list)
        assert len(result["actions"]) >= 2  # At least the complete actions should be preserved
        assert result["actions"][0] == {"type": "click", "element": "button1"}
        assert result["actions"][1] == {"type": "type", "text": "hello"}

    def test_parse_api_response_unescaped_quotes_in_value(self) -> None:
        """Test parsing JSON with unescaped quotes in string values"""
        response = self._create_mock_response('{"message": "He said "hello" to me"}')
        result = parse_api_response(response)
        assert result == {"message": 'He said "hello" to me'}
