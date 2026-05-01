"""Tests for login-block credential subscript emission (SKY-9340).

Two layers under test:

1. ``infer_credential_subscript_for_emit`` — a pure helper. Given an action +
   goal template + block_type + credential_param_keys, returns ``(root, sub)``
   for credential fills or ``None`` to defer to existing emission paths.

2. ``_action_to_stmt`` — the emitter. Given a ``page.fill`` action and the
   credential context threaded from ``_build_block_fn``, must emit
   ``context.parameters[<root>][<sub>]`` instead of flat
   ``context.parameters[<picker's name>]`` for credential fills.

Each scenario fixture reflects a shape we observed in production logs.
"""

from __future__ import annotations

import libcst as cst

from skyvern.core.script_generations.deterministic_field_naming import (
    infer_credential_subscript_for_emit,
)
from skyvern.core.script_generations.generate_script import _action_to_stmt
from skyvern.webeye.actions.actions import ActionType


def _login_action(
    text: str,
    intention: str,
    *,
    field_name: str | None = None,
    action_id: str = "a1",
    xpath: str = "//input",
) -> dict:
    action: dict = {
        "action_type": ActionType.INPUT_TEXT,
        "text": text,
        "intention": intention,
        "action_id": action_id,
        "xpath": xpath,
    }
    if field_name is not None:
        action["field_name"] = field_name
    return action


def _emit(action: dict, **kwargs) -> str:
    stmt = _action_to_stmt(action, task={}, **kwargs)
    module = cst.Module(body=[stmt] if isinstance(stmt, cst.BaseStatement) else [cst.SimpleStatementLine([stmt])])
    return module.code


# ---------------------------------------------------------------------------
# Layer 1: helper (infer_credential_subscript_for_emit)
# ---------------------------------------------------------------------------


def test_helper_dotted_jinja_canonical_subkey() -> None:
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_xxx_username", intention="username for login"),
        goal_template="type {{my_credential.username}}. type {{my_credential.password}}.",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick == ("my_credential", "username")


def test_helper_dotted_jinja_alias_email_to_username() -> None:
    """`{{cred.email}}` — alias map normalizes `email` → `username`."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("user@example.com", intention="enter the email"),
        goal_template="Email: {{my_credential.email}}\nPassword: {{my_credential.password}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick == ("my_credential", "username")


def test_helper_dotted_jinja_with_whitespace_around_dot() -> None:
    """Jinja allows whitespace around `.` in attribute access. Without the
    `\\s*` tolerance in `_JINJA_DOTTED_RE`, multi-credential disambiguation
    silently fails when the goal template uses `{{ root . sub }}` form."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_xxx_username", intention="username"),
        goal_template="user: {{ cred_a . username }} pass: {{cred_b.password}}",
        block_type="login",
        credential_param_keys=frozenset({"cred_a", "cred_b"}),
    )
    assert pick == ("cred_a", "username")


def test_helper_intention_keyword_username() -> None:
    """Two Jinja vars in the goal but only one CREDENTIAL_ID param —
    intention keyword routes to username."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention="What is the login email address?"),
        goal_template="extra: {{extra_login_instructions}} {'cred': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick == ("my_credential", "username")


def test_helper_intention_keyword_password() -> None:
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention="What is the login password?"),
        goal_template="login with {{my_credential}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick == ("my_credential", "password")


def test_helper_placeholder_token_text_marks_username() -> None:
    """The recorded action text — when the agent fills from a credential —
    is a `placeholder_<random>_<subkey>` token. Suffix match on the sub-key
    is the canonical structural signal; intention text is just a fallback."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_AbCd_username", intention="enter the value"),
        goal_template="login with {{my_credential}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick == ("my_credential", "username")


def test_helper_token_with_extra_suffix_is_not_misclassified() -> None:
    """The placeholder parser extracts the full sub-key after the random
    component and validates against `KNOWN_CREDENTIAL_SUBKEYS`. A token like
    `placeholder_AbCd_backup_username` extracts `backup_username`, which is
    NOT in the canonical set, so it falls through."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_AbCd_backup_username", intention=""),
        goal_template="login with {{my_credential}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_token_for_unknown_subkey_falls_through() -> None:
    """Same guarantee for entirely novel sub-keys like `email`. The runtime
    credential dict only contains canonical keys; routing here would
    KeyError. Must fall through."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_AbCd_email", intention=""),
        goal_template="login with {{my_credential}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_does_not_fire_outside_login_block() -> None:
    """Same intention text outside a login block must not route to credential
    subscript — protects non-login forms with username fields."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("alice", intention="What is the username?"),
        goal_template="Add a new user: {{my_credential}}",
        block_type="task",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_does_not_fire_without_credential_param() -> None:
    """No CREDENTIAL_ID-typed param → helper returns None; existing flat path
    runs in the emitter."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("alice", intention="username"),
        goal_template="login with {{some_other_param}}",
        block_type="login",
        credential_param_keys=frozenset(),
    )
    assert pick is None


def test_helper_does_not_fire_for_totp_intention() -> None:
    """Verification-code intention must not route — TOTP has its own paths."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention="Enter the 6-digit verification code"),
        goal_template="{'cred': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_does_not_fire_for_one_time_password_intention() -> None:
    """`one-time password` contains the substring `password` but is TOTP-flavored.
    The denylist short-circuits before the password keyword check."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention="Enter the one-time password"),
        goal_template="{'cred': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_does_not_fire_for_totp_token_with_passcode_intention() -> None:
    """Regression: a `placeholder_<r>_totp` token whose intention contains
    `passcode` must NOT fall through to the intention path. `passcode` matches
    `_PASSWORD_KEYWORDS` and isn't in the TOTP denylist, so without the
    placeholder-is-authoritative short-circuit the helper would route the
    user's password into the OTP field."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("placeholder_AbCd_totp", intention="enter passcode"),
        goal_template="{'cred': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_does_not_fire_for_unknown_intention() -> None:
    pick = infer_credential_subscript_for_emit(
        action=_login_action("Alice", intention="first name"),
        goal_template="{'cred': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


def test_helper_multi_credential_falls_through_without_dotted_jinja() -> None:
    """Two CREDENTIAL_ID params and no dotted Jinja — helper can't disambiguate."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention="What is the login password?"),
        goal_template="login with {{cred_a}} or {{cred_b}}",
        block_type="login",
        credential_param_keys=frozenset({"cred_a", "cred_b"}),
    )
    assert pick is None


def test_helper_multi_credential_resolved_by_dotted_jinja() -> None:
    """Two CREDENTIAL_ID params, but goal has `{{cred_a.username}}` only."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("alice", intention="username"),
        goal_template="type {{cred_a.username}}",
        block_type="login",
        credential_param_keys=frozenset({"cred_a", "cred_b"}),
    )
    assert pick == ("cred_a", "username")


def test_helper_multi_credential_same_subkey_falls_through() -> None:
    """Two credentials both expose `.username` — cannot safely pick one."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("alice", intention="username for login"),
        goal_template="type {{cred_a.username}} or {{cred_b.username}}",
        block_type="login",
        credential_param_keys=frozenset({"cred_a", "cred_b"}),
    )
    assert pick is None


def test_helper_dotted_jinja_unrecognized_subkey_falls_through() -> None:
    """`{{cred.session_token}}` — `session_token` is not a known credential
    sub-key and has no alias."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("abc123", intention="session token"),
        goal_template="Token: {{my_credential.session_token}}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None


# ---------------------------------------------------------------------------
# Layer 2: emitter (_action_to_stmt)
# ---------------------------------------------------------------------------


def test_emitter_credential_field_emits_nested_subscript() -> None:
    """Smoke: a login-block credential field emits
    `context.parameters['my_credential']['username']`."""
    code = _emit(
        _login_action(
            "placeholder_xxx_username", intention="What is the login email?", field_name="phantom_login_email"
        ),
        block_type="login",
        goal_template="login with {{my_credential}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['username']" in code
    assert "context.parameters['phantom_login_email']" not in code


def test_emitter_password_field_emits_nested() -> None:
    code = _emit(
        _login_action("placeholder_xxx_password", intention="What is the login password?", field_name="phantom_pw"),
        block_type="login",
        goal_template="login with {{my_credential}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['password']" in code
    assert "context.parameters['phantom_pw']" not in code


def test_emitter_dotted_jinja_alias_routes_email_to_username() -> None:
    code = _emit(
        _login_action("user@example.com", intention="enter the email"),
        block_type="login",
        goal_template="Email: {{my_credential.email}}\nPassword: {{my_credential.password}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['username']" in code


def test_emitter_dotted_jinja_custom_credential_name() -> None:
    """A workflow whose CREDENTIAL_ID param has a non-`credentials` name."""
    code = _emit(
        _login_action("placeholder_xxx_password", intention="password for login"),
        block_type="login",
        goal_template="type {{bot_password.username}}. type {{bot_password.password}}.",
        credential_param_keys=frozenset({"bot_password"}),
    )
    assert "context.parameters['bot_password']['password']" in code


def test_emitter_falls_through_to_flat_when_no_credential_param() -> None:
    """No CREDENTIAL_ID-typed param → emitter uses the picker's flat field_name."""
    code = _emit(
        _login_action("alice", intention="username", field_name="login_username"),
        block_type="login",
        goal_template="login with {{some_other_param}}",
        credential_param_keys=frozenset(),
    )
    assert "context.parameters['login_username']" in code
    assert "['username']" not in code  # no nested emission


def test_emitter_falls_through_outside_login_block() -> None:
    code = _emit(
        _login_action("alice", intention="username", field_name="customer_username"),
        block_type="task",
        goal_template="add user {{my_credential}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['customer_username']" in code
    assert "['username']" not in code


def test_emitter_totp_sequence_path_unchanged() -> None:
    """Multi-cell TOTP actions hit the existing `get_totp_digit` branch."""
    action = _login_action("1", intention="totp digit 0", field_name="totp_code")
    action["totp_timing_info"] = {"is_totp_sequence": True, "action_index": 0}
    code = _emit(
        action,
        block_type="login",
        goal_template="{'cred': '{{my_credential}}'}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "page.get_totp_digit(context, 'totp_code', 0)" in code
    assert "['totp']" not in code
    assert "context.parameters['my_credential']['" not in code


def test_emitter_one_time_password_intention_does_not_emit_subscript() -> None:
    """Single-cell TOTP with intention "Enter one-time password" — TOTP
    denylist prevents the substring `password` from triggering the
    credential branch."""
    code = _emit(
        _login_action("", intention="Enter the one-time password", field_name="totp_field"),
        block_type="login",
        goal_template="{'cred': '{{my_credential}}'}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']" not in code
    assert "context.parameters['totp_field']" in code


def test_emitter_falls_through_for_ambiguous_intention() -> None:
    code = _emit(
        _login_action("Alice", intention="first name", field_name="customer_name"),
        block_type="login",
        goal_template="{'cred': '{{my_credential}}'}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['customer_name']" in code
    assert "['username']" not in code


def test_emitter_default_args_preserve_existing_behavior() -> None:
    """Existing call sites that don't thread credential context still work."""
    code = _emit(
        _login_action("alice", intention="username", field_name="login_username"),
    )
    assert "context.parameters['login_username']" in code


# ---------------------------------------------------------------------------
# Cross-run scenario fixtures
# ---------------------------------------------------------------------------
#
# Each fixture corresponds to a shape we observed in production. Names are
# generic — see the SKY-9340 plan / per-run reports for the original mapping.


def test_cross_run_two_jinja_vars_one_credential() -> None:
    """Goal has two Jinja vars (one credential, one free-text). Picker would
    bind to the wrong one (Rule 1 bails on length-2 intersection); emitter
    overrides with nested credential."""
    code = _emit(
        _login_action(
            "placeholder_xxx_username",
            intention="What is the login email address?",
            field_name="extra_login_instructions",
        ),
        block_type="login",
        goal_template=(
            "If there are extra login conditions: {{extra_login_instructions}} {'cred': '{{my_credential}}'}"
        ),
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['username']" in code


def test_cross_run_dotted_jinja_custom_credential_name() -> None:
    """Goal uses dotted `{{<custom_name>.username}}` and the workflow's
    CREDENTIAL_ID param uses a non-`credentials` name."""
    code = _emit(
        _login_action("placeholder_xxx_username", intention="username", field_name="bot_password"),
        block_type="login",
        goal_template="type {{bot_password.username}}. type {{bot_password.password}}.",
        credential_param_keys=frozenset({"bot_password"}),
    )
    assert "context.parameters['bot_password']['username']" in code


def test_cross_run_dotted_jinja_email_alias() -> None:
    code = _emit(
        _login_action("user@example.com", intention="enter email", field_name="phantom"),
        block_type="login",
        goal_template="Email: {{my_credential.email}}\nPassword: {{my_credential.password}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['username']" in code


def test_cross_run_singular_credential_jinja() -> None:
    """Goal has a single Jinja `{{credential}}` (singular), CREDENTIAL_ID
    param matches."""
    code = _emit(
        _login_action("placeholder_xxx_username", intention="What is the username?", field_name="phantom"),
        block_type="login",
        goal_template="{'credential': '{{credential}}'}",
        credential_param_keys=frozenset({"credential"}),
    )
    assert "context.parameters['credential']['username']" in code


def test_cross_run_no_jinja_in_goal_single_credential() -> None:
    """Goal mentions credentials in prose, no Jinja templating, single
    CREDENTIAL_ID param."""
    code = _emit(
        _login_action("placeholder_xxx_password", intention="password", field_name="phantom"),
        block_type="login",
        goal_template="log in using the credentials provided",
        credential_param_keys=frozenset({"enterprise_credential"}),
    )
    assert "context.parameters['enterprise_credential']['password']" in code


def test_cross_run_numbered_credential_param_name() -> None:
    """CREDENTIAL_ID param named `<word>_1` (numbered)."""
    code = _emit(
        _login_action("placeholder_xxx_username", intention="username", field_name="user_username"),
        block_type="login",
        goal_template="{'credentials_1': '{{credentials_1}}'}",
        credential_param_keys=frozenset({"credentials_1"}),
    )
    assert "context.parameters['credentials_1']['username']" in code


def test_cross_run_replaces_reviewer_hardcoded_literal() -> None:
    """A workflow whose cached login was previously rewritten by the AI
    reviewer to hardcode a literal email. Phase 1 emits the credential
    subscript instead, untying it from the static account."""
    code = _emit(
        _login_action("placeholder_xxx_username", intention="username for the login", field_name="login_email"),
        block_type="login",
        goal_template="login with {{my_credential}}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['username']" in code


def test_cross_run_replaces_reviewer_stripped_value() -> None:
    """A workflow whose cached login had `value=` stripped by the reviewer
    (every fill falling back to AI). Phase 1 restores deterministic
    credential emission."""
    code = _emit(
        _login_action("placeholder_xxx_password", intention="password to log in", field_name="phantom"),
        block_type="login",
        goal_template="log in using the saved credentials\n{'cred': '{{my_credential}}'}",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert "context.parameters['my_credential']['password']" in code


def test_cross_run_no_credential_id_passes_through() -> None:
    """Workflow without a CREDENTIAL_ID-typed param — picker's flat name
    flows through unchanged."""
    code = _emit(
        _login_action("placeholder_xxx", intention="The username for logging in", field_name="login_username"),
        block_type="login",
        goal_template="log in to the partner portal",
        credential_param_keys=frozenset(),
    )
    assert "context.parameters['login_username']" in code
    assert "['username']" not in code


def test_cross_run_no_input_actions_helper_returns_none() -> None:
    """Workflow whose recorded login had no INPUT_TEXT actions (cached login
    is goto+complete, fully delegated to runtime agent). The emitter is
    never reached for fills; the helper still returns None on an
    intention-less action."""
    pick = infer_credential_subscript_for_emit(
        action=_login_action("", intention=""),
        goal_template="{'my_credential': '{{my_credential}}'}",
        block_type="login",
        credential_param_keys=frozenset({"my_credential"}),
    )
    assert pick is None
