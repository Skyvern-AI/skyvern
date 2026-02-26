from skyvern.forge.agent import _find_verification_code_action


class TestFindVerificationCodeAction:
    def test_finds_mfa_action_at_any_position(self) -> None:
        """MFA action is 3rd of 4 actions (email, password, MFA, submit)."""
        actions = [
            {"action_type": "INPUT_TEXT", "id": "AAAX", "reasoning": "Fill email field", "text": "user@example.com"},
            {"action_type": "INPUT_TEXT", "id": "AAAZ", "reasoning": "Fill password field", "text": "pa$w0rd"},
            {
                "action_type": "INPUT_TEXT",
                "id": "AAAb",
                "reasoning": "MFA code must be entered into the MFA input",
                "text": "123456",
            },
            {
                "action_type": "CLICK",
                "id": "AAAc",
                "reasoning": "Submit the entered credentials and MFA code",
                "text": None,
            },
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "AAAb"
        assert result["text"] == "123456"

    def test_finds_totp_keyword(self) -> None:
        actions = [
            {
                "action_type": "INPUT_TEXT",
                "id": "A1",
                "reasoning": "Enter TOTP code from authenticator",
                "text": "654321",
            },
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["text"] == "654321"

    def test_finds_otp_keyword(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "B1", "reasoning": "Input OTP value", "text": "111111"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None

    def test_skips_click_actions_mentioning_mfa(self) -> None:
        """CLICK actions mentioning MFA should be ignored — avoids dupes."""
        actions = [
            {"action_type": "CLICK", "id": "AAAc", "reasoning": "Submit the MFA code form", "text": None},
        ]
        result = _find_verification_code_action(actions)
        assert result is None

    def test_skips_click_only_matches_input_text(self) -> None:
        """Only the INPUT_TEXT action should match, not the CLICK that also mentions MFA."""
        actions = [
            {"action_type": "INPUT_TEXT", "id": "AAAb", "reasoning": "Enter MFA code", "text": "123456"},
            {"action_type": "CLICK", "id": "AAAc", "reasoning": "Submit MFA form", "text": None},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "AAAb"

    def test_returns_none_for_empty_list(self) -> None:
        assert _find_verification_code_action([]) is None

    def test_returns_none_when_no_mfa_actions(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "X1", "reasoning": "Fill the name field", "text": "John"},
            {"action_type": "CLICK", "id": "X2", "reasoning": "Submit the form", "text": None},
        ]
        assert _find_verification_code_action(actions) is None

    def test_case_insensitive(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "C1", "reasoning": "Enter the MFA Code here", "text": "555555"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None

    def test_handles_missing_reasoning(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "D1", "text": "999"},
            {"action_type": "INPUT_TEXT", "id": "D2", "reasoning": "Enter OTP", "text": "888"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "D2"
