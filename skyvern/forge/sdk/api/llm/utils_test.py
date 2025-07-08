"""
Tests for methods in utils.py that use commentjson.loads
"""

import json
from unittest.mock import Mock

import litellm
import pytest

from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.api.llm.utils import _fix_cutoff_json, _fix_unescaped_quotes_in_json, parse_api_response


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


def test_fixing_json_with_unescaped_quotes() -> None:
    json_string_with_unescaped_quotes = """
{
  "actions": [
    {
      "reasoning": "The "Full name" field is a mandatory field.",
      "confidence_float": 1,
      "action_type": "INPUT_TEXT",
      "id": 21,
      "text": "Kerem Yilmaz",
      "file_url": null,
      "option": null
    }
  ]
}
"""
    expected_parsed_json = {
        "actions": [
            {
                "reasoning": 'The "Full name" field is a mandatory field.',
                "confidence_float": 1,
                "action_type": "INPUT_TEXT",
                "id": 21,
                "text": "Kerem Yilmaz",
                "file_url": None,
                "option": None,
            }
        ]
    }
    fixed_json_string = _fix_unescaped_quotes_in_json(json_string_with_unescaped_quotes)
    assert json.loads(fixed_json_string) == expected_parsed_json


# Test with nested JSON structure containing unescaped quotes within strings
def test_fixing_json_with_nested_unescaped_quotes() -> None:
    json_string_with_unescaped_quotes = """
{
  "name": "Alice",
  "profile": {
    "details": "She said "yes" when asked."
  }
}
"""
    expected_output = {"name": "Alice", "profile": {"details": 'She said "yes" when asked.'}}
    fixed_json_string = _fix_unescaped_quotes_in_json(json_string_with_unescaped_quotes)
    assert json.loads(fixed_json_string) == expected_output


# Test with multiple unescaped quotes in a single string
def test_multiple_unescaped_quotes_in_string() -> None:
    json_string_with_unescaped_quotes = """
{
  "summary": "John said "I can't do this anymore" and left."
}
"""
    expected_output = {"summary": 'John said "I can\'t do this anymore" and left.'}
    fixed_json_string = _fix_unescaped_quotes_in_json(json_string_with_unescaped_quotes)
    assert json.loads(fixed_json_string) == expected_output


# Test with unescaped quotes immediately followed by a JSON structure character
def test_unescaped_quotes_followed_by_structure_char() -> None:
    json_string_with_unescaped_quotes = """
{
  "dialogue": "He exclaimed "Wow!" and walked away."
}
"""
    expected_output = {"dialogue": 'He exclaimed "Wow!" and walked away.'}
    fixed_json_string = _fix_unescaped_quotes_in_json(json_string_with_unescaped_quotes)
    assert json.loads(fixed_json_string) == expected_output


# Test with valid JSON that does not need fixing
def test_valid_json_no_fix_needed() -> None:
    json_string = """
{
  "status": "Active",
  "message": "This is a \\"properly escaped\\" JSON string."
}
"""
    # Expect the parsed output to match the expected JSON dictionary
    expected_output = {"status": "Active", "message": 'This is a "properly escaped" JSON string.'}
    assert json.loads(_fix_unescaped_quotes_in_json(json_string)) == expected_output


# Test with corrupted JSON structure that cannot be fixed by this function
def test_corrupted_json_structure() -> None:
    json_string_with_corrupted_structure = """
{
  "corrupted": "This JSON "structure is missing" a closing bracket
"""
    # Since the structure is incomplete, expect the json loading to raise an error
    with pytest.raises(json.JSONDecodeError):
        json.loads(_fix_unescaped_quotes_in_json(json_string_with_corrupted_structure))


def test_unescaped_quotes_in_json() -> None:
    json_string_with_unescaped_quotes = """
{\n    \"actions\": [\n        {\n            \"reasoning\": \"The 'First Name' field is a required field for the job application, and the user_data contains the user's first name 'Chris P.'. Therefore, I should input the user's first name to satisfy this mandatory field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 36,\n            \"text\": \"Chris P.\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Last Name' field is a required field for the job application, and the user_data contains the user's last name 'Bacon'. Therefore, I should input the user's last name to satisfy this mandatory field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 39,\n            \"text\": \"Bacon\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Email' field is a required field for the job application, and the user_data contains the user's email 'ChrisP@Bacon.com'. Therefore, I should input the user's email to satisfy this mandatory field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 42,\n            \"text\": \"ChrisP@Bacon.com\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Phone' field is not a required field, but the user_data contains the user's phone number '+1 647 888 0408'. Since this information is available, I should input the user's phone number to provide complete information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 44,\n            \"text\": \"+1 647 888 0408\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Resume/CV' field is a required field for the job application, and the user_data contains a URL for the user's resume 'resume_url'. Therefore, I should upload the user's resume from the provided URL to satisfy this mandatory field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"UPLOAD_FILE\",\n            \"id\": 47,\n            \"text\": null,\n            \"file_url\": \"resume_url\",\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'LinkedIn Profile' field is not a required field, but the user_data contains the user's LinkedIn profile URL 'https://www.linkedin.com/in/Chris P.Bacon'. Since this information is available, I should input the user's LinkedIn profile URL to provide complete information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 59,\n            \"text\": \"https://www.linkedin.com/in/Chris P.Bacon\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'How did you hear about us?' field is a required field for the job application. Since the user_data does not contain this specific information, I should provide a generic but relevant answer to allow the application to proceed, such as 'Online job board'.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 64,\n            \"text\": \"Online job board\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'What was the most important book you've read in the last 12 months?' field is a required field for the job application. Since the user_data does not contain this specific information, I should leave this field blank to avoid providing inaccurate or misleading information.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 67,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Would you rather be a \"Jack-of-all-trades\" or \"Master-of-one\"? Tell us your answer, and why?' field is a required field for the job application. Since the user_data does not contain this specific information, I should leave this field blank to avoid providing inaccurate or misleading information.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 70,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'We are looking for \"intrapreneurs.\" Ownership is our #1 Core Value. Are you an intrapreneur? Do you naturally embody ownership? Why?' field is a required field for the job application. Based on the user's professional summary in the user_data, which mentions 'As a Sales & Strategy Leader, I am dedicated to driving revenue growth, customer satisfaction, and market expansion for SaaS organizations', I can provide a relevant answer highlighting the user's ownership and leadership qualities.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 73,\n            \"text\": \"Yes, I am an intrapreneur who naturally embodies ownership. Throughout my career, I have demonstrated a proven track record of driving revenue growth, customer satisfaction, and market expansion for SaaS organizations. I take ownership of my responsibilities and strive to deliver innovative solutions that create value for the organization and its stakeholders.\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Some people are Pirates and some people are Imperial Naval Officers. On the spectrum between the two, which one are you? Why? If neither, how would you describe yourself?' field is a required field for the job application. Since the user_data does not contain specific information to answer this question accurately, I should leave this field blank to avoid providing inaccurate or misleading information.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 76,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'If you are based in the USA, please confirm which State you currently reside in. (Select N/A if you are based outside the USA)' field is a required field for the job application. Based on the user_data, which shows the user's location as 'Toronto, ON, Canada', I should select the 'N/A' option since the user is not based in the USA.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 79,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"N/A\",\n                \"index\": 1,\n                \"value\": \"N/A\"\n            }\n        }\n    ]\n}
"""

    expected_output = {
        "actions": [
            {
                "reasoning": "The 'First Name' field is a required field for the job application, and the user_data contains the user's first name 'Chris P.'. Therefore, I should input the user's first name to satisfy this mandatory field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 36,
                "text": "Chris P.",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Last Name' field is a required field for the job application, and the user_data contains the user's last name 'Bacon'. Therefore, I should input the user's last name to satisfy this mandatory field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 39,
                "text": "Bacon",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Email' field is a required field for the job application, and the user_data contains the user's email 'ChrisP@Bacon.com'. Therefore, I should input the user's email to satisfy this mandatory field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 42,
                "text": "ChrisP@Bacon.com",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Phone' field is not a required field, but the user_data contains the user's phone number '+1 647 888 0408'. Since this information is available, I should input the user's phone number to provide complete information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 44,
                "text": "+1 647 888 0408",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Resume/CV' field is a required field for the job application, and the user_data contains a URL for the user's resume 'resume_url'. Therefore, I should upload the user's resume from the provided URL to satisfy this mandatory field.",
                "confidence_float": 1.0,
                "action_type": "UPLOAD_FILE",
                "id": 47,
                "text": None,
                "file_url": "resume_url",
                "option": None,
            },
            {
                "reasoning": "The 'LinkedIn Profile' field is not a required field, but the user_data contains the user's LinkedIn profile URL 'https://www.linkedin.com/in/Chris P.Bacon'. Since this information is available, I should input the user's LinkedIn profile URL to provide complete information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 59,
                "text": "https://www.linkedin.com/in/Chris P.Bacon",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'How did you hear about us?' field is a required field for the job application. Since the user_data does not contain this specific information, I should provide a generic but relevant answer to allow the application to proceed, such as 'Online job board'.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 64,
                "text": "Online job board",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'What was the most important book you've read in the last 12 months?' field is a required field for the job application. Since the user_data does not contain this specific information, I should leave this field blank to avoid providing inaccurate or misleading information.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 67,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": 'The \'Would you rather be a "Jack-of-all-trades" or "Master-of-one"? Tell us your answer, and why?\' field is a required field for the job application. Since the user_data does not contain this specific information, I should leave this field blank to avoid providing inaccurate or misleading information.',
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 70,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'We are looking for \"intrapreneurs.\" Ownership is our #1 Core Value. Are you an intrapreneur? Do you naturally embody ownership? Why?' field is a required field for the job application. Based on the user's professional summary in the user_data, which mentions 'As a Sales & Strategy Leader, I am dedicated to driving revenue growth, customer satisfaction, and market expansion for SaaS organizations', I can provide a relevant answer highlighting the user's ownership and leadership qualities.",
                "confidence_float": 0.8,
                "action_type": "INPUT_TEXT",
                "id": 73,
                "text": "Yes, I am an intrapreneur who naturally embodies ownership. Throughout my career, I have demonstrated a proven track record of driving revenue growth, customer satisfaction, and market expansion for SaaS organizations. I take ownership of my responsibilities and strive to deliver innovative solutions that create value for the organization and its stakeholders.",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Some people are Pirates and some people are Imperial Naval Officers. On the spectrum between the two, which one are you? Why? If neither, how would you describe yourself?' field is a required field for the job application. Since the user_data does not contain specific information to answer this question accurately, I should leave this field blank to avoid providing inaccurate or misleading information.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 76,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'If you are based in the USA, please confirm which State you currently reside in. (Select N/A if you are based outside the USA)' field is a required field for the job application. Based on the user_data, which shows the user's location as 'Toronto, ON, Canada', I should select the 'N/A' option since the user is not based in the USA.",
                "confidence_float": 1.0,
                "action_type": "SELECT_OPTION",
                "id": 79,
                "text": None,
                "file_url": None,
                "option": {"label": "N/A", "index": 1, "value": "N/A"},
            },
        ]
    }

    assert json.loads(_fix_unescaped_quotes_in_json(json_string_with_unescaped_quotes)) == expected_output


def test_cutoff_json_1() -> None:
    cutoff_json = """
{\n    \"actions\": [\n        {\n            \"reasoning\": \"The resume/CV is a mandatory field for the job application. The user has provided a resume URL in the user_data, so I should upload that file to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"UPLOAD_FILE\",\n            \"id\": 13,\n            \"text\": null,\n            \"file_url\": \"randomurl.com\",\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Full name' field is a mandatory field, and the user's first and last name are provided in the user_data. I should input the full name to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 17,\n            \"text\": \"Chris P Bacon\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Email' field is a mandatory field, and the user's email address is provided in the user_data. I should input the email to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 21,\n            \"text\": \"chris.p@bacon.com\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Phone' field is a mandatory field, and the user's phone number is provided in the user_data. I should input the phone number to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 25,\n            \"text\": \"3211234567\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Current location' field is not a mandatory field, but the user's city and state are provided in the user_data. I should input the location to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 28,\n            \"text\": \"Nanya, BZ\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Current company' field is not a mandatory field, but the user's current employer is provided in the user_data. I should input the company name to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 31,\n            \"text\": \"Skyvern\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'LinkedIn Profile URL' field is not mandatory, but the user has provided their LinkedIn profile link in the user_data. I should input the URL to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 35,\n            \"text\": \"random linkedin url\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'How did you hear about this position?' question is a mandatory field. Since the user_data does not provide this information, I should select a common option like 'Job board (LinkedIn, Indeed, etc.)' to allow the application to proceed.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 43,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"Job board (LinkedIn, Indeed, etc.)\",\n                \"index\": 6,\n                \"value\": \"Job board (LinkedIn, Indeed, etc.)\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'Have you previously applied for a job at MLT?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 60,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"No\",\n                \"index\": 2,\n                \"value\": \"No\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'Have you been previously employed by MLT?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 68,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"No\",\n                \"index\": 2,\n                \"value\": \"No\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'What is your desired salary expectation range?' question is a mandatory field. Since the user_data does not provide this information, I should provide a generic response indicating openness to discuss compensation based on the role and company's budget.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 76,\n            \"text\": \"I am open to discussing compensation based on the role's requirements and the company's budget.\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'What type(s) of employment are you interested in?' question is a mandatory field. Since the user_data does not provide this information, I should select the 'Full-time' option to allow the application to proceed, as this is a common employment type.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"CLICK\",\n            \"id\": 81,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Mailing address' field is a mandatory field, and the user's address, city, state, and zip code are provided in the user_data. I should input the full mailing address to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 95,\n            \"text\": \"123 Bacon Court, Nanya, BZ 01234\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(1) College/University attended' field is a mandatory field, and the user's education details are provided in the user_data. I should input the first college/university name to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 100,\n            \"text\": \"Skyvern Uni\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(1) College/University location (City, State)' field is a mandatory field, and the user's education details do not provide this information. To allow the application to proceed, I should leave this field blank.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 104,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(1) Enrollment name if different from application OR insert N/A' field is a mandatory field, and the user's education details do not provide this information. To allow the application to proceed, I should input 'N/A'.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 108,\n            \"text\": \"N/A\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(1) Highest degree' field is a mandatory field, and the user's education details provide the degree type for Skyvern Uni. I should select the appropriate degree option to complete this required field.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 112,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"Bachelors\",\n                \"index\": 2,\n                \"value\": \"Bachelors\"\n            }\n        },\n        {\n            \"reasoning\": \"The '(1) Graduated?' field is a mandatory field, and the user's education details do not provide this information for Skyvern Uni. To allow the application to proceed, I should select 'Yes' as a reasonable assumption.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 120,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"Yes\",\n                \"index\": 1,\n                \"value\": \"Yes\"\n            }\n        },\n        {\n            \"reasoning\": \"The '(1) GPA' field is a mandatory field, and the user's education details do not provide this information for Skyvern Uni. To allow the application to proceed, I should leave this field blank.\",\n            \"confidence_float\": 0.8,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 124,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) College/University attended' field is not a mandatory field, and the user's education details provide a second college/university name. I should input the second college/university name to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 127,\n            \"text\": \"Skyvern Uni\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) College/University location (City, State)' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 130,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) Enrollment name if different from application name OR insert N/A' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should input 'N/A'.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 133,\n            \"text\": \"N/A\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) Highest degree' field is not a mandatory field, and the user's education details provide the degree type for Skyvern Uni. I should select the appropriate degree option to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 136,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"Baccalaureate\",\n                \"index\": 5,\n                \"value\": \"Other\"\n            }\n        },\n        {\n            \"reasoning\": \"The '(2) If you selected \\\"Other,\\\" please describe OR insert N/A' field is not a mandatory field, and I selected 'Other' for the '(2) Highest degree' field. I should input 'Baccalaureate' to provide accurate information.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 139,\n            \"text\": \"Baccalaureate\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) College/University dates of attendance' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 142,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) Graduated?' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 145,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The '(2) GPA' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 148,\n            \"text\": \"\",\n            \"file_url\": null,\n            \"option\": null\n        },\n        {\n            \"reasoning\": \"The 'Have you been convicted of a crime within the past five (5) years?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed, as it is a reasonable assumption in the absence of any contrary information.\",\n            \"confidence_float\": 0.9,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 153,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"No\",\n                \"index\": 2,\n                \"value\": \"No\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'Are you legally eligible for employment in the United States?' question is a mandatory field, and the user_data indicates that the user is authorized to work in the US. I should select 'Yes' to complete this required field accurately.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 162,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"Yes\",\n                \"index\": 1,\n                \"value\": \"Yes\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'Will you now or in the future require MLT to commence (\\\"sponsor\\\") an immigration case in order to employ you?' question is a mandatory field, and the user_data indicates that the user does not need sponsorship. I should select 'No' to complete this required field accurately.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"SELECT_OPTION\",\n            \"id\": 166,\n            \"text\": null,\n            \"file_url\": null,\n            \"option\": {\n                \"label\": \"No\",\n                \"index\": 2,\n                \"value\": \"No\"\n            }\n        },\n        {\n            \"reasoning\": \"The 'Additional information' section is optional, and the user has provided a cover letter in the user_data. I should input the cover letter text to provide additional context and demonstrate the user's interest in the role.\",\n            \"confidence_float\": 1.0,\n            \"action_type\": \"INPUT_TEXT\",\n            \"id\": 169,\n            \"text\": \"With great enthusiasm, I am excited to apply for the Talent Placement and Operations Manager position at Management Leadership for Tomorrow. As an experienced professional with a diverse background in operations, project management, and talent acquisition, I am confident in my ability to contribute significantly to your organization's talent placement initiatives.\\n\\nThroughout my career, I have honed my skills in candidate sourcing, outreach, and matching, enabling me to identify suitable candidates for various partner roles based on their skills, experiences, and career aspirations. My experience in overseeing the onboarding process for
"""
    expected_output = {
        "actions": [
            {
                "reasoning": "The resume/CV is a mandatory field for the job application. The user has provided a resume URL in the user_data, so I should upload that file to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "UPLOAD_FILE",
                "id": 13,
                "text": None,
                "file_url": "randomurl.com",
                "option": None,
            },
            {
                "reasoning": "The 'Full name' field is a mandatory field, and the user's first and last name are provided in the user_data. I should input the full name to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 17,
                "text": "Chris P Bacon",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Email' field is a mandatory field, and the user's email address is provided in the user_data. I should input the email to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 21,
                "text": "chris.p@bacon.com",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Phone' field is a mandatory field, and the user's phone number is provided in the user_data. I should input the phone number to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 25,
                "text": "3211234567",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Current location' field is not a mandatory field, but the user's city and state are provided in the user_data. I should input the location to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 28,
                "text": "Nanya, BZ",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Current company' field is not a mandatory field, but the user's current employer is provided in the user_data. I should input the company name to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 31,
                "text": "Skyvern",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'LinkedIn Profile URL' field is not mandatory, but the user has provided their LinkedIn profile link in the user_data. I should input the URL to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 35,
                "text": "random linkedin url",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'How did you hear about this position?' question is a mandatory field. Since the user_data does not provide this information, I should select a common option like 'Job board (LinkedIn, Indeed, etc.)' to allow the application to proceed.",
                "confidence_float": 0.8,
                "action_type": "SELECT_OPTION",
                "id": 43,
                "text": None,
                "file_url": None,
                "option": {
                    "label": "Job board (LinkedIn, Indeed, etc.)",
                    "index": 6,
                    "value": "Job board (LinkedIn, Indeed, etc.)",
                },
            },
            {
                "reasoning": "The 'Have you previously applied for a job at MLT?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed.",
                "confidence_float": 0.9,
                "action_type": "SELECT_OPTION",
                "id": 60,
                "text": None,
                "file_url": None,
                "option": {"label": "No", "index": 2, "value": "No"},
            },
            {
                "reasoning": "The 'Have you been previously employed by MLT?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed.",
                "confidence_float": 0.9,
                "action_type": "SELECT_OPTION",
                "id": 68,
                "text": None,
                "file_url": None,
                "option": {"label": "No", "index": 2, "value": "No"},
            },
            {
                "reasoning": "The 'What is your desired salary expectation range?' question is a mandatory field. Since the user_data does not provide this information, I should provide a generic response indicating openness to discuss compensation based on the role and company's budget.",
                "confidence_float": 0.8,
                "action_type": "INPUT_TEXT",
                "id": 76,
                "text": "I am open to discussing compensation based on the role's requirements and the company's budget.",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'What type(s) of employment are you interested in?' question is a mandatory field. Since the user_data does not provide this information, I should select the 'Full-time' option to allow the application to proceed, as this is a common employment type.",
                "confidence_float": 0.9,
                "action_type": "CLICK",
                "id": 81,
                "text": None,
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Mailing address' field is a mandatory field, and the user's address, city, state, and zip code are provided in the user_data. I should input the full mailing address to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 95,
                "text": "123 Bacon Court, Nanya, BZ 01234",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(1) College/University attended' field is a mandatory field, and the user's education details are provided in the user_data. I should input the first college/university name to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 100,
                "text": "Skyvern Uni",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(1) College/University location (City, State)' field is a mandatory field, and the user's education details do not provide this information. To allow the application to proceed, I should leave this field blank.",
                "confidence_float": 0.8,
                "action_type": "INPUT_TEXT",
                "id": 104,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(1) Enrollment name if different from application OR insert N/A' field is a mandatory field, and the user's education details do not provide this information. To allow the application to proceed, I should input 'N/A'.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 108,
                "text": "N/A",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(1) Highest degree' field is a mandatory field, and the user's education details provide the degree type for Skyvern Uni. I should select the appropriate degree option to complete this required field.",
                "confidence_float": 1.0,
                "action_type": "SELECT_OPTION",
                "id": 112,
                "text": None,
                "file_url": None,
                "option": {"label": "Bachelors", "index": 2, "value": "Bachelors"},
            },
            {
                "reasoning": "The '(1) Graduated?' field is a mandatory field, and the user's education details do not provide this information for Skyvern Uni. To allow the application to proceed, I should select 'Yes' as a reasonable assumption.",
                "confidence_float": 0.8,
                "action_type": "SELECT_OPTION",
                "id": 120,
                "text": None,
                "file_url": None,
                "option": {"label": "Yes", "index": 1, "value": "Yes"},
            },
            {
                "reasoning": "The '(1) GPA' field is a mandatory field, and the user's education details do not provide this information for Skyvern Uni. To allow the application to proceed, I should leave this field blank.",
                "confidence_float": 0.8,
                "action_type": "INPUT_TEXT",
                "id": 124,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) College/University attended' field is not a mandatory field, and the user's education details provide a second college/university name. I should input the second college/university name to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 127,
                "text": "Skyvern Uni",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) College/University location (City, State)' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 130,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) Enrollment name if different from application name OR insert N/A' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should input 'N/A'.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 133,
                "text": "N/A",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) Highest degree' field is not a mandatory field, and the user's education details provide the degree type for Skyvern Uni. I should select the appropriate degree option to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "SELECT_OPTION",
                "id": 136,
                "text": None,
                "file_url": None,
                "option": {"label": "Baccalaureate", "index": 5, "value": "Other"},
            },
            {
                "reasoning": "The '(2) If you selected \"Other,\" please describe OR insert N/A' field is not a mandatory field, and I selected 'Other' for the '(2) Highest degree' field. I should input 'Baccalaureate' to provide accurate information.",
                "confidence_float": 1.0,
                "action_type": "INPUT_TEXT",
                "id": 139,
                "text": "Baccalaureate",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) College/University dates of attendance' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 142,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) Graduated?' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.",
                "confidence_float": 0.9,
                "action_type": "SELECT_OPTION",
                "id": 145,
                "text": None,
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The '(2) GPA' field is not a mandatory field, and the user's education details do not provide this information for the second college/university. To avoid providing inaccurate information, I should leave this field blank.",
                "confidence_float": 0.9,
                "action_type": "INPUT_TEXT",
                "id": 148,
                "text": "",
                "file_url": None,
                "option": None,
            },
            {
                "reasoning": "The 'Have you been convicted of a crime within the past five (5) years?' question is a mandatory field. Since the user_data does not provide this information, I should select 'No' to allow the application to proceed, as it is a reasonable assumption in the absence of any contrary information.",
                "confidence_float": 0.9,
                "action_type": "SELECT_OPTION",
                "id": 153,
                "text": None,
                "file_url": None,
                "option": {"label": "No", "index": 2, "value": "No"},
            },
            {
                "reasoning": "The 'Are you legally eligible for employment in the United States?' question is a mandatory field, and the user_data indicates that the user is authorized to work in the US. I should select 'Yes' to complete this required field accurately.",
                "confidence_float": 1.0,
                "action_type": "SELECT_OPTION",
                "id": 162,
                "text": None,
                "file_url": None,
                "option": {"label": "Yes", "index": 1, "value": "Yes"},
            },
            {
                "reasoning": "The 'Will you now or in the future require MLT to commence (\"sponsor\") an immigration case in order to employ you?' question is a mandatory field, and the user_data indicates that the user does not need sponsorship. I should select 'No' to complete this required field accurately.",
                "confidence_float": 1.0,
                "action_type": "SELECT_OPTION",
                "id": 166,
                "text": None,
                "file_url": None,
                "option": {"label": "No", "index": 2, "value": "No"},
            },
        ]
    }

    assert _fix_cutoff_json(cutoff_json, 15805) == expected_output
