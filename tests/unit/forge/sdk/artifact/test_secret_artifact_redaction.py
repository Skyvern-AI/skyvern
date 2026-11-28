import base64
import gc
import io
import json
import random
import time
import tracemalloc
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, ArtifactManager, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils import raw_secret_redaction, run_data_scrub
from skyvern.utils.secret_redaction import (
    REDACTED_SECRET_PLACEHOLDER,
    REDACTION_FAILED_STUB,
    redact_credential_shapes_from_bytes,
    redact_secrets_from_json_bytes,
)
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    TEST_ORGANIZATION_ID,
    create_fake_step,
    create_fake_workflow_run_block,
)

TEST_STEP_ID = "step_secret_redaction"
PLACEHOLDER_ID = "placeholder_k3y9"
_R = REDACTED_SECRET_PLACEHOLDER
# Assembled from parts so secret scanners do not flag the fixtures.
_JWT = ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"))
_PRIVATE_KEY = "\n".join(
    (
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA1b2c3",
        "abcDEF123==",
        "-----END RSA " + "PRIVATE KEY-----",
    )
)
_API_KEY = "sk-" + "proj-AbCdEf0123456789XyZ"
_TRUNCATED_PRIVATE_KEY = (
    "-----BEGIN RSA " + "PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA" + "1b2c3d4e5f6g7h8i" * 3 + "\nAbCdEf0123+/=="
)
# Markup, code and prose next to credential keywords that the shape scrub must leave as they are.
_UNTOUCHED_PAGE = (
    '<label for="password">Enter your password: </label>'
    '<input type="password" name="password" autocomplete="current-password"><input name="api_key"><input name=token>'
    '<input type="hidden" name="csrf_token" value="8f14e45fceea167a5a36dedd4bea2543">'
    '<span class="token attr-value">x</span><div class="sk-cube-grid sk-cube-grid-item-12">'
    '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kg">'
    '<script src="/static/js/main.3f2a9c8b1d4e5f6a7b8c9d0e1f2a3b4c.js?v=8f14e45fceea167a5a36dedd4bea2543"></script>'
    "<style>.token:hover{color:red}.token:nth-child(2n+1){margin:0}</style>"
    "<script>const password = document.getElementById('pw').value; let token = getToken();</script>"
    "<script>input.onchange = function (e) { save({password:e.value}) }</script>"
    "<p>Paste the -----BEGIN RSA " + "PRIVATE KEY----- header and the rest of the file here please.</p>"
    f"<p>Forgot password? Reset your password: click here. Type {PLACEHOLDER_ID}, password: {PLACEHOLDER_ID}</p>"
)
_CREDENTIALS_PAGE = (
    f'<script>window.cfg = {{session: "{_JWT}", client: "{_API_KEY}"}};</script>'
    '<a href="/reset?token=a8F3kq92LmZx77&amp;u=1">reset</a><pre>aws AKIAIOSFODNN7EXAMPLE\n'
    f"{_PRIVATE_KEY}</pre>"
)
_SCRUBBED_CREDENTIALS_PAGE = (
    f'<script>window.cfg = {{session: "{_R}", client: "{_R}"}};</script>'
    f'<a href="/reset?token={_R}&amp;u=1">reset</a><pre>aws {_R}\n{_R}</pre>'
)
_LOG_LINE = 'INFO login Authorization: Bearer abc123DEF456ghi789 password="Sun,shine(1)!" user=someone\n'
_JSON_FIELDS = {"password": "", "token_count": 123, "max_tokens": 4096, "page_token": "abc123"}
# Key spellings and value shapes the scrub must read, one per line as in a log: a password's quoted value of any
# shape, camelCase, snake_case, kebab and upper-case names, and compound names ending in a credential name.
_KEYWORD_VARIANT_VALUES = (
    ('password="', "correcthorsebatterystaple", '"'),
    ('passcode="', "12345678", '"'),
    ('password="', "Abcd 1234", '"'),
    ("password='", "Ab\tcd 12", "'"),
    ('accessToken="', "Ab12Cd34Ef56", '"'),
    ('{"clientSecret": "', "cs_9f8e7d6c5b", '"}'),
    ("refreshToken: ", "rt-1a2b3c4d5e", ""),
    ("secret_key=", "sk4Ab9Zq81", ""),
    ("AWS_SECRET_ACCESS_KEY=", "wJalr" + "XUtnFEMI/K7MDENG", ""),
    ("passwd=", "Pa55word", ""),
    ("pwd=", "hunter22", ""),
    ('password="', "can'tGuess123", '"'),
    ("api_key=", "correcthorsebatterystaple", ""),
    ("X-Api-Key: ", "abcdefghijklmnop", ""),
    ('{"db_password": "', "Hunter22x", '"}'),
    ("Authorization: Bearer ", "abcdefghijklmnopqrstuv", ""),
)
_KEYWORD_VARIANTS_LOG = "\n".join(f"{key}{value}{end}" for key, value, end in _KEYWORD_VARIANT_VALUES)
_SCRUBBED_KEYWORD_VARIANTS_LOG = "\n".join(f"{key}{_R}{end}" for key, _value, end in _KEYWORD_VARIANT_VALUES)
_UNTOUCHED_KEYWORD_VARIANTS_LOG = ' nextPageToken="CAoQAA1x" user=someone\n'


_OAUTH1_AUTHORIZATION = (
    'Authorization: OAuth oauth_consumer_key="abc123456", oauth_token="tok123456", oauth_signature="sig123456"'
)
_SIGV4_AUTHORIZATION = (
    "Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261126/us-east-1/s3/aws4_request, "
    "SignedHeaders=host;x-amz-date, Signature=abcdef0123456789"
)
# One row per credential shape the scrub must read; `<R>` is where the placeholder goes.
_CREDENTIAL_SHAPE_ROWS = (
    ("compound_token_name", "api_token=abcd1234efgh", "api_token=<R>"),
    ("upper_token_name", "GITHUB_TOKEN=abcdefghijklmnop", "GITHUB_TOKEN=<R>"),
    ("jwt_secret_name", "JWT_SECRET=supersecretvalue", "JWT_SECRET=<R>"),
    ("spaced_value_compound_password", '{"new_password": "correct horse battery staple"}', '{"new_password": "<R>"}'),
    ("camelcase_password_name", '{"confirmPassword": "Hunter22x"}', '{"confirmPassword": "<R>"}'),
    ("cookie_in_a_header_dict", '{"headers": {"Cookie": "sid=Zq9xWv7Lm; a=1"}}', '{"headers": {"Cookie": "<R>"}}'),
    ("cookie_in_a_python_repr", "{'Cookie': 'sid=Zq9xWv7Lm'}", "{'Cookie': '<R>'}"),
    ("cookie_line_in_pre", "<pre>Cookie: sid=Zq9xWv7Lm</pre>", "<pre>Cookie: <R></pre>"),
    (
        "authorization_after_log_prefix",
        "INFO login Authorization: Bearer abcdefghijklmnopqrstuv",
        "INFO login Authorization: Bearer <R>",
    ),
    ("cookie_after_log_prefix", "INFO req Cookie: sid=Zq9xWv7Lm", "INFO req Cookie: <R>"),
    ("authorization_without_scheme", '{"Authorization": "abcdef123456XYZ"}', '{"Authorization": "<R>"}'),
    (
        "authorization_in_a_cell",
        "<td>Authorization: Bearer abcdefghijklmnop</td>",
        "<td>Authorization: Bearer <R></td>",
    ),
    (
        "digest_authorization",
        'Authorization: Digest username="bob", nonce="abc", response="6629fae49393a05397450978507c4ef1"',
        "Authorization: Digest <R>",
    ),
    (
        "token_in_url_fragment",
        "https://x.example/cb#access_token=" + "abcdef" + "123456",
        "https://x.example/cb#access_token=<R>",
    ),
    ("hash_rocket", "'password' => 'Zq9xWv7Lm'", "'password' => '<R>'"),
    ("ruby_symbol_key", ':password => "Zq9xWv7Lm"', ':password => "<R>"'),
    ("php_variable", "$password = 'Zq9xWv7Lm';", "$password = '<R>';"),
    ("percent_encoded_assignment", "password%3DZq9xWv7Lm", "password%3D<R>"),
    ("backtick_quote", "password = `Zq9x Wv7Lm`", "password = `<R>`"),
    ("triple_quote", 'password = """Zq9x Wv7Lm"""', 'password = """<R>"""'),
    ("python_bytes_prefix", "kwargs={'password': b'Zq9xWv7Lm'}", "kwargs={'password': b'<R>'}"),
    ("yaml_block_scalar", "password: |\n  Zq9xWv7Lm", "password: |\n  <R>"),
    (
        "yaml_block_scalar_spaced_and_multiline",
        "db:\n  password: >-\n    correct horse\n\n    battery staple\n  user: admin",
        "db:\n  password: >-\n    <R>\n  user: admin",
    ),
    (
        "yaml_block_scalar_in_json_text",
        '{"config": "password: |\\n  correct horse battery\\nuser: admin"}',
        '{"config": "password: |\\n  <R>\\nuser: admin"}',
    ),
    (
        "yaml_block_scalar_in_a_script_string_after_an_apostrophe",
        '<script>var c = "db:\\n  note: it\'s\\n  password: |\\n    Zq9xWv7Lm"; var keep = "keepme123";</script><p>Rest</p>',
        '<script>var c = "db:\\n  note: it\'s\\n  password: |\\n    <R>"; var keep = "keepme123";</script><p>Rest</p>',
    ),
    (
        "yaml_block_scalar_in_a_single_quoted_repr_after_a_double_quoted_value",
        "cfg={'c': 'db:\\n  host: \"h\"\\n  password: |\\n    ab\"Zq9xWv7Lm\\n'}",
        "cfg={'c': 'db:\\n  host: \"h\"\\n  password: |\\n    <R>\\n'}",
    ),
    (
        "yaml_block_scalar_ends_at_its_json_string",
        'x={"config": "password: |\\n  correct horse\\n  battery", "debug": 1}',
        'x={"config": "password: |\\n  <R>", "debug": 1}',
    ),
    ("array_value", '{"password": ["Zq9xWv7Lm"]}', '{"password": ["<R>"]}'),
    (
        "every_array_element",
        '{"password": ["firstsecret", null, "ab", "secondsecret"]}',
        '{"password": ["<R>", null, "ab", "<R>"]}',
    ),
    ("numeric_array_element", "password = [12345678, 87654321]", "password = [<R>, <R>]"),
    ("kebab_header_token", "X-Vault-Token: " + "hvs.abcdef" + "123456", "X-Vault-Token: <R>"),
    ("upper_kebab_token", "PRIVATE-TOKEN: " + "glabc" + "123456789", "PRIVATE-TOKEN: <R>"),
    ("webhook_secret", "webhook_secret=whsec_abc123def", "webhook_secret=<R>"),
    ("totp_seed", "totp_secret=JBSWY3DPEHPK3PXP", "totp_secret=<R>"),
    ("php_session_cookie", "PHPSESSID=abcdef123456", "PHPSESSID=<R>"),
    ("pass_as_whole_name", "pass=Zq9xWv7Lm", "pass=<R>"),
    ("django_session_cookie", "sessionid=Zq9xWv7Lm", "sessionid=<R>"),
    (
        "password_confirmation_twins",
        "password_confirmation=Zq9xWv7Lm passwordAgain=Zq9xWv7Lm password2=Zq9xWv7Lm password_repeat=Zq9xWv7Lm",
        "password_confirmation=<R> passwordAgain=<R> password2=<R> password_repeat=<R>",
    ),
    (
        "password_confirmation_twins_in_json",
        '{"passwordConfirmation": "Zq9xWv7Lm", "password_confirm": "Zq9xWv7Lm"}',
        '{"passwordConfirmation": "<R>", "password_confirm": "<R>"}',
    ),
    ("authorization_any_scheme_word", "Authorization: SSWS 00abcDEF123456xyz", "Authorization: SSWS <R>"),
    ("authorization_scheme_with_tchar", "Authorization: A+B abc123def456", "Authorization: A+B <R>"),
    (
        "authorization_in_a_code_span_after_a_log_prefix",
        "INFO login Authorization: `Bearer abcdef0123456789` ok",
        "INFO login Authorization: `Bearer <R>` ok",
    ),
    ("authorization_in_a_code_span", "Authorization: `SSWS 00abcdef0123456789xyz`", "Authorization: `SSWS <R>`"),
    ("authorization_three_letter_scheme", "Authorization: MAC abc123def456", "Authorization: MAC <R>"),
    ("proxy_authorization_scheme_with_tchar", "Proxy-Authorization: A+B abc123def456", "Proxy-Authorization: A+B <R>"),
    (
        "proxy_authorization_three_letter_scheme",
        "Proxy-Authorization: MAC abc123def456",
        "Proxy-Authorization: MAC <R>",
    ),
    (
        "authorization_any_scheme_word_in_header_dict",
        '{"Authorization": "ApiKey abc123def456", "x": "Bot Zq9xWv7Lm"}',
        '{"Authorization": "<R>", "x": "Bot Zq9xWv7Lm"}',
    ),
    (
        "authorization_oauth1_auth_params",
        _OAUTH1_AUTHORIZATION,
        "Authorization: OAuth <R>",
    ),
    (
        "authorization_oauth1_auth_params_in_json",
        json.dumps({"h": _OAUTH1_AUTHORIZATION, "url": "https://example.test/keep"}),
        json.dumps({"h": "Authorization: OAuth <R>", "url": "https://example.test/keep"}),
    ),
    # The scheme word holds a letter and a digit, so the keyword scan reads it as the header's value too.
    ("authorization_sigv4_auth_params", _SIGV4_AUTHORIZATION, "Authorization: <R> <R>"),
    (
        "authorization_sigv4_auth_params_in_json",
        json.dumps({"h": _SIGV4_AUTHORIZATION, "url": "https://example.test/keep"}),
        json.dumps({"h": "Authorization: <R> <R>", "url": "https://example.test/keep"}),
    ),
    (
        "digest_ends_at_its_auth_params",
        'h={"Authorization": "Digest username=\\"u\\", response=\\"6629fae4\\""} url=https://x.test/a status=200',
        'h={"Authorization": "Digest <R>"} url=https://x.test/a status=200',
    ),
    ("cookie_header_every_cookie", "Cookie: a=1; sid=Zq9xWv7Lm", "Cookie: <R>"),
    ("set_cookie_in_a_header_option", '-H "Set-Cookie: sid=Zq9xWv7Lm; Path=/"', '-H "Set-Cookie: <R>"'),
    ("set_cookie_quoted_value", 'Set-Cookie: sid="Zq9xWv7Lm"; Path=/', "Set-Cookie: <R>"),
    (
        "set_cookie_escaped_quoted_value_in_json",
        '{"h": "Set-Cookie: sid=\\"Zq9xWv7Lm\\"; Path=/", "ua": "Mozilla/5.0 (X11)"}',
        '{"h": "Set-Cookie: <R>", "ua": "Mozilla/5.0 (X11)"}',
    ),
    (
        "cookie_header_in_a_dict",
        '{"Cookie": "sid=Zq9xWv7Lm", "Accept": "text/html"}',
        '{"Cookie": "<R>", "Accept": "text/html"}',
    ),
    ("newline_escape_in_quoted", '{"password": "line1\\nZq9xWv7Lm"}', '{"password": "<R>"}'),
    ("unicode_escape_value", '["password=\\u5bc6Zq9xWv7Lm"]', '["password=<R>"]'),
    (
        "html_entity_json",
        '<div data-props="{&quot;password&quot;:&quot;Zq9xWv7Lm&quot;}">',
        '<div data-props="{&quot;password&quot;:&quot;<R>&quot;}">',
    ),
    ("password_input_value", '<input value="Zq9xWv7Lm" type="password">', '<input value="<R>" type="password">'),
    (
        "password_input_named_as_a_field",
        '<input name="passwordField" value="Zq9xWv7Lm">',
        '<input name="passwordField" value="<R>">',
    ),
    ("url_userinfo", "postgres://user:Zq9xWv7Lm@db/x", "postgres://user:<R>@db/x"),
    (
        "url_userinfo_long_scheme",
        "postgresql+psycopg://user:Zq9xWv7Lm@db/x",
        "postgresql+psycopg://user:<R>@db/x",
    ),
    ("url_userinfo_one_letter_scheme", "x://user:secret123@host.example.test", "x://user:<R>@host.example.test"),
    ("url_userinfo_no_username", "redis://:secret123@host/0", "redis://:<R>@host/0"),
    ("url_userinfo_300_char_password", "redis://user:" + "Zq9x" * 75 + "@host/0", "redis://user:<R>@host/0"),
    (
        "password_input_value_after_an_attribute_naming_value",
        '<input type="password" data-help="set value=visible" value="actualsecret123">',
        '<input type="password" data-help="set value=visible" value="<R>">',
    ),
    (
        "password_input_value_holding_a_greater_than_in_double_quotes",
        '<input type="password" value="abc>secret123">',
        '<input type="password" value="<R>">',
    ),
    (
        "password_input_value_holding_a_greater_than_in_single_quotes",
        "<input type='password' value='abc>secret123'>",
        "<input type='password' value='<R>'>",
    ),
    # Backslashes with no closing quote; an ambiguous read of each one used to take seconds per line.
    ("backslash_run_no_close", 'password="' + "\\\\" * 25, None),
    (
        "cookie_value_ending_in_equals_in_json",
        '{"h": "Cookie: \\"sid=abc=\\"", "url": "https://example.test/keep", "status": "ok"}',
        '{"h": "Cookie: \\"<R>\\"", "url": "https://example.test/keep", "status": "ok"}',
    ),
    (
        "cookie_value_ending_in_equals_in_text",
        'h="Cookie: \\"sid=abc=\\"", url=https://example.test/keep',
        'h="Cookie: \\"<R>\\"", url=https://example.test/keep',
    ),
    (
        "yaml_block_on_a_later_line_of_a_json_string",
        '{"cfg": "db:\\n  password: |\\n    hunter22secret", "url": "https://example.test/keep", "model": "m-1"}',
        '{"cfg": "db:\\n  password: |\\n    <R>", "url": "https://example.test/keep", "model": "m-1"}',
    ),
    (
        "yaml_block_in_a_one_line_page",
        "<pre>app:\\n  password: |\\n    hunter22secret</pre><p>Order total 42</p>",
        "<pre>app:\\n  password: |\\n    <R></pre><p>Order total 42</p>",
    ),
    (
        "yaml_block_ends_at_its_element_close",
        "<pre>password: |\n  hunter22secret</pre><p>Order total 42</p>",
        "<pre>password: |\n  <R></pre><p>Order total 42</p>",
    ),
    (
        "yaml_block_double_quoted_key",
        '"password": |\n  correct horse 42\nuser: admin',
        '"password": |\n  <R>\nuser: admin',
    ),
    (
        "yaml_block_after_an_odd_quote",
        'TV 5" screen\ndb:\n  password: |\n    ab"cd hunter22x\n  user: admin',
        'TV 5" screen\ndb:\n  password: |\n    <R>\n  user: admin',
    ),
    (
        "yaml_block_in_a_single_quoted_repr",
        "cfg={'c': 'db:\\n  password: |\\n    ab\"hunter22secret\\n'}",
        "cfg={'c': 'db:\\n  password: |\\n    <R>\\n'}",
    ),
    (
        "yaml_block_in_a_single_quoted_js_string",
        "var c = 'db:\\n  password: |\\n    x\"y\\n    hunter22secret\\n';",
        "var c = 'db:\\n  password: |\\n    <R>\\n';",
    ),
    (
        "yaml_block_holding_a_closing_tag",
        "password: |\n  abc</b>hunter22x\nuser: admin",
        "password: |\n  <R>\nuser: admin",
    ),
    ("yaml_block_cr_line_breaks", "password: |\r  hunter22secret\ruser: admin", "password: |\r  <R>\ruser: admin"),
    ("cookie_quoted_value_then_a_comma", 'Cookie: sid="abc123xyz", theme=dark', "Cookie: <R>"),
    ("cookie_quoted_value_with_a_comma", 'Cookie: sid="abc,123xyz"', "Cookie: <R>"),
    ("cookie_quoted_value_with_a_space", 'Set-Cookie: sid="abc 123xyz"; Path=/', "Set-Cookie: <R>"),
    ("cookie_quoted_value_with_a_semicolon", 'Cookie: sid="abc;123xyz"', "Cookie: <R>"),
    ("cookie_quoted_value_with_a_backslash", 'Cookie: sid="abc\\d123xyz"', "Cookie: <R>"),
    ("cookie_quoted_value_with_an_escaped_quote", 'Cookie: sid="abc\\"123xyz"', "Cookie: <R>"),
    ("cookie_quoted_value_with_octal_escapes", 'Cookie: sid="abc\\073123\\054xyz"', "Cookie: <R>"),
    ("cookie_several_quoted_values", 'Set-Cookie: a="x1", sid="abc123xyz", b=y', "Set-Cookie: <R>"),
    ("cookie_unquoted_value_with_escapes", "Cookie: sid=abc\\/123\\x2fxyz", "Cookie: <R>"),
    (
        "cookie_value_ends_at_an_escaped_line_break",
        "Cookie: sid=Zq9xWv7Lm\\nGET /keep-this-path",
        "Cookie: <R>\\nGET /keep-this-path",
    ),
    (
        "cookie_unclosed_quote_ends_at_an_escaped_line_break",
        'Cookie: sid="Zq9xWv7Lm\\r\\nGET /keep-this-path',
        "Cookie: <R>\\r\\nGET /keep-this-path",
    ),
    (
        "cookie_quoted_value_with_a_space_in_json",
        '{"h": "Cookie: sid=\\"abc 123xyz\\", theme=dark", "url": "https://example.test/keep"}',
        '{"h": "Cookie: <R>", "url": "https://example.test/keep"}',
    ),
    *[
        (
            f"yaml_block_in_json_in_a_json_string_{row_id}",
            json.dumps({"resp": json.dumps(inner)}),
            json.dumps({"resp": json.dumps(scrubbed)}),
        )
        for row_id, inner, scrubbed in (
            ("nested", {"config": "db:\n  password: |\n    hunter22secret"}, {"config": "db:\n  password: |\n    <R>"}),
            (
                "members_after",
                {"config": "db:\n  password: |\n    hunter22secret", "url": "https://example.test/keep"},
                {"config": "db:\n  password: |\n    <R>", "url": "https://example.test/keep"},
            ),
            ("first_line", {"config": "password: |\n  hunter22secret"}, {"config": "password: |\n  <R>"}),
            (
                "private_key",
                {"config": "private_key: |\n  hunter22secret\n  morelines42"},
                {"config": "private_key: |\n  <R>"},
            ),
        )
    ],
    (
        "yaml_block_in_json_in_a_json_string_in_text",
        "resp=" + json.dumps(json.dumps({"config": "db:\n  password: |\n    hunter22secret", "n": "x"})),
        "resp=" + json.dumps(json.dumps({"config": "db:\n  password: |\n    <R>", "n": "x"})),
    ),
    (
        "jwt_as_a_json_key",
        json.dumps({_JWT: 1, "url": "https://example.test/keep"}),
        '{"<R>": 1, "url": "https://example.test/keep"}',
    ),
    ("api_key_as_a_json_key", json.dumps({"ghp_" + "Ab1" * 12: "x"}), '{"<R>": "x"}'),
    (
        "url_keys_with_credentials",
        json.dumps(
            {
                "https://example.test/cb?access_token=abcdef123456&x=1": {"ms": 12},
                "https://example.test/a?token=Zq9xWv7Lm": 3,
                "https://user:Zq9xWv7Lm@example.test/x": 4,
                "https://example.test/keep": 5,
            }
        ),
        json.dumps(
            {
                "https://example.test/cb?access_token=<R>&x=1": {"ms": 12},
                "https://example.test/a?token=<R>": 3,
                "https://user:<R>@example.test/x": 4,
                "https://example.test/keep": 5,
            }
        ),
    ),
    (
        "header_and_assignment_as_json_keys",
        json.dumps({"Authorization: Bearer abcdefghijklmnop": True, "password=Zq9xWv7Lm": 1}),
        json.dumps({"Authorization: Bearer <R>": True, "password=<R>": 1}),
    ),
    (
        "reset_password_token_input",
        '<input type="hidden" name="reset_password_token" value="Zq9xWv7Lm2pQ">',
        '<input type="hidden" name="reset_password_token" value="<R>">',
    ),
    (
        "password_reset_token_input",
        '<input type="hidden" name="password_reset_token" value="Zq9xWv7Lm2pQ">',
        '<input type="hidden" name="password_reset_token" value="<R>">',
    ),
    (
        "devise_reset_password_token_input",
        '<input type="hidden" name="user[reset_password_token]" id="user_reset_password_token" value="Zq9xWv7Lm2pQ">',
        '<input type="hidden" name="user[reset_password_token]" id="user_reset_password_token" value="<R>">',
    ),
    ("password_that_reads_as_a_key_name", "password: Password123", "password: <R>"),
    (
        "key_name_password_after_a_comma",
        "Log in with the shared account, password: Password123",
        "Log in with the shared account, password: <R>",
    ),
    ("key_name_password_in_a_query", "?user=bob&password=Secret123&next=/", "?user=bob&password=<R>&next=/"),
    ("env_password_with_an_ampersand", "DB_PASSWORD=ab&cdefgh12", "DB_PASSWORD=<R>"),
    ("password_with_braces", "password: Qz9{mK2pL}", "password: <R>"),
    ("password_with_parentheses", "password: x(Y)z12345", "password: <R>"),
    ("cli_password_with_a_comma", "--password=P@s,sw0rdXY", "--password=<R>"),
    ("password_with_an_ampersand_inside", "password: Tr0ub4dor&3xyz", "password: <R>"),
    (
        "userinfo_url_with_escaped_slashes",
        'INFO body={"u": "https:\\/\\/bob:Zq9xWv7Lm@db.example\\/x"}',
        'INFO body={"u": "https:\\/\\/bob:<R>@db.example\\/x"}',
    ),
    ("authorization_in_a_ruby_hash", '{"Authorization" => "SSWS 00abcDEF123456"}', '{"Authorization" => "SSWS <R>"}'),
)
# Text that names no credential; each is stored as it is.
_KEPT_TEXT_ROWS = (
    ("password_assigned_a_variable", "{password: newPassword}"),
    ("password_assigned_a_call", "password = getPassword();"),
    ("password_assigned_an_env_reference", "password=${DB_PASSWORD}"),
    (
        "work_authorization",
        '{"work_authorization": "Authorized to work in the United States", "workAuthorization": "US Citizen"}',
    ),
    ("constant_holding_a_message", 'const BAD_TOKEN = "The token you entered is invalid.";'),
    ("ternary_labels", 'show ? "Hide password" : "Show password"'),
    ("key_assigned_to_itself", "login({email: email, password: password})"),
    ("prose_label", "Forgot password: contact support"),
    ("password_hint_input", '<input name="password_hint" value="your first pet">'),
    ("show_password_input", '<input id="show-password" type="checkbox" value="Show password">'),
    ("input_placeholder_holding_a_greater_than", '<input type="text" placeholder="a > b" value="Show password"> 5 > 3'),
    ("url_port_and_bare_address", "http://host:8080/path user@host"),
    ("authorization_prose_in_a_cell", "<td>Authorization: Approved on 11/03/2024</td>"),
    ("call", "token = uuid.uuid4()"),
    ("call_with_digit", "password = self.get_pw_v2()"),
    ("member_chain", 'const password = document.getElementById("pw").value; save({password:e.value})'),
    ("css_selectors", ".token:hover{color:red} .password:focus-visible{} input.password:invalid{} #password:focus{}"),
    ("cursors_and_counts", 'nextPageToken="CAoQAA1x" page_token=abc123def "max_tokens": 4096 "token_count": 123456'),
    ("type_words_and_masks", "password: string; password: SecretStr\nPassword: Required\npassword: ********"),
    ("spaced_opaque_value", '{"secret": "This is not a secret message", "is_secret": "maybe yes"}'),
    ("password_ui_names", '{"forgot_password": "Forgot your password?", "password_hint": "your first pet"}'),
    (
        "cookie_prose",
        'Cookie: accept all to continue\n{"cookie_banner": "We use cookies", "cookie": "This site uses cookies"}',
    ),
    ("page_code_writing_a_cookie", 'document.cookie = "theme=dark; Path=/";'),
    (
        "skyvern_session_ids",
        '{"event": "step done", "browser_session_id": "pbs_123456789"} browser_session_id=pbs_12345 session_id=wcc_1234567',
    ),
    ("authorization_prose", "Authorization: required for all requests"),
    ("authorization_one_letter_scheme", "Authorization: X abc123def456"),
    ("proxy_authorization_one_letter_scheme", "Proxy-Authorization: X abc123def456"),
    ("prior_authorization_number", "Prior Authorization: 3 PA2024-00123"),
    ("work_authorization_letter", "Work Authorization: Y H1B-2025 status"),
    ("authorization_ranking", "Authorization: #1 priority2025"),
    ("yaml_block_scalar_under_a_non_credential_key", "password_hint: |\n  my first pet's name\nnotes: >\n  a b c"),
    ("long_scheme_url_with_a_port", "postgresql+psycopg://db.internal:5432/app and vscode-insiders://x:1/y"),
    ("cookie_prose_with_a_quote", 'Set the cookie: "remember me" to stay signed in'),
)


def _nested_json_text(value: object, layers: int) -> str:
    text = json.dumps(value)
    for _ in range(layers - 1):
        text = json.dumps(text)
    return text


def _decode_json_text_layers(value: object) -> object:
    if isinstance(value, str) and value != REDACTED_SECRET_PLACEHOLDER and value.lstrip()[:1] in ("{", "["):
        return _decode_json_text_layers(json.loads(value))
    if isinstance(value, dict):
        return {key: _decode_json_text_layers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_text_layers(item) for item in value]
    return value


def _strict_json(data: bytes) -> object:
    def reject(constant: str) -> object:
        raise ValueError(f"non-standard JSON constant {constant}")

    return json.loads(data.decode("utf-8"), parse_constant=reject)


class _FakeStorage:
    def __init__(self) -> None:
        self.stored: list[tuple[Artifact | None, bytes]] = []

    def build_uri(self, **_: object) -> str:
        return "s3://bucket/artifact"

    def build_workflow_run_block_uri(self, **_: object) -> str:
        return "s3://bucket/workflow-run-block-artifact"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        self.stored.append((artifact, data))

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        self.stored.append((artifact, Path(path).read_bytes()))


class _FakeArtifactDatabase:
    async def create_artifact(
        self,
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        **kwargs: object,
    ) -> Artifact:
        now = datetime.now(UTC)
        return Artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=str(kwargs["organization_id"]),
            step_id=kwargs.get("step_id"),
            task_id=kwargs.get("task_id"),
            workflow_run_id=kwargs.get("workflow_run_id"),
            workflow_run_block_id=kwargs.get("workflow_run_block_id"),
            run_id=kwargs.get("run_id"),
            created_at=now,
            modified_at=now,
        )

    async def bulk_create_artifacts(self, artifact_models: list[ArtifactModel]) -> list[Artifact]:
        return [
            await self.create_artifact(
                model.artifact_id,
                ArtifactType(model.artifact_type),
                model.uri,
                organization_id=model.organization_id,
                task_id=model.task_id,
                step_id=model.step_id,
                workflow_run_id=model.workflow_run_id,
            )
            for model in artifact_models
        ]


@pytest.fixture
def artifact_redaction_setup(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> _FakeStorage:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=True,
            secrets={"password": "secret-value", PLACEHOLDER_ID: "placeholder-value"},
        ),
    )
    skyvern_context.reset()
    skyvern_context.set(SkyvernContext(organization_id=TEST_ORGANIZATION_ID, workflow_run_id="wr_redact"))
    yield storage
    skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data", "expected"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", None),
        (ArtifactType.HASHED_HREF_MAP, b'{"h1": "https://example.test/callback?token=secret-value"}', None),
        (ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP, b'{"el_1": "input[value=secret-value]"}', None),
        pytest.param(
            ArtifactType.HTML_SCRAPE,
            (_UNTOUCHED_PAGE + _CREDENTIALS_PAGE).encode(),
            (_UNTOUCHED_PAGE + _SCRUBBED_CREDENTIALS_PAGE).encode(),
            id="credential_shapes_in_html",
        ),
        # Past the size that sends the shape scrub to a worker thread.
        pytest.param(
            ArtifactType.HTML,
            (" " * 300_000 + _CREDENTIALS_PAGE).encode(),
            (" " * 300_000 + _SCRUBBED_CREDENTIALS_PAGE).encode(),
            id="credential_shapes_in_large_html",
        ),
        pytest.param(
            ArtifactType.LLM_PROMPT,
            json.dumps({"page": _UNTOUCHED_PAGE + _CREDENTIALS_PAGE, "n": 12345678901234567890.5}).encode(),
            json.dumps({"page": _UNTOUCHED_PAGE + _SCRUBBED_CREDENTIALS_PAGE, "n": 12345678901234567890.5}).encode(),
            id="credential_shapes_in_prompt_json",
        ),
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            _LOG_LINE.encode(),
            f'INFO login Authorization: Bearer {_R} password="{_R}" user=someone\n'.encode(),
            id="credential_shapes_in_log",
        ),
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            (_KEYWORD_VARIANTS_LOG + _UNTOUCHED_KEYWORD_VARIANTS_LOG).encode(),
            (_SCRUBBED_KEYWORD_VARIANTS_LOG + _UNTOUCHED_KEYWORD_VARIANTS_LOG).encode(),
            id="keyword_variants_in_log",
        ),
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps({"log": _KEYWORD_VARIANTS_LOG + _UNTOUCHED_KEYWORD_VARIANTS_LOG}).encode(),
            json.dumps({"log": _SCRUBBED_KEYWORD_VARIANTS_LOG + _UNTOUCHED_KEYWORD_VARIANTS_LOG}).encode(),
            id="keyword_variants_in_json_text",
        ),
        # JSON text inside a string five times, past the escaped quotes the pattern reads directly.
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            f"args={_nested_json_text({'password': 'Abcd123456'}, 5)}\n".encode(),
            f"args={_nested_json_text({'password': _R}, 5)}\n".encode(),
            id="credential_in_json_text_five_layers_deep_in_log",
        ),
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps({"args": _nested_json_text({"password": "Abcd123456"}, 4)}).encode(),
            json.dumps({"args": _nested_json_text({"password": _R}, 4)}).encode(),
            id="credential_in_json_text_five_layers_deep_in_json",
        ),
        *[
            pytest.param(
                ArtifactType.SKYVERN_LOG,
                f"{text}\n".encode(),
                None if expected is None else f"{expected.replace('<R>', _R)}\n".encode(),
                id=row_id,
            )
            for row_id, text, expected in _CREDENTIAL_SHAPE_ROWS
        ],
        *[
            pytest.param(ArtifactType.SKYVERN_LOG, f"{text}\n".encode(), None, id=f"kept_{row_id}")
            for row_id, text in _KEPT_TEXT_ROWS
        ],
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps({"password": 83920174, "pin": 839201, "max_tokens": 4096}).encode(),
            json.dumps({"password": _R, "pin": 839201, "max_tokens": 4096}).encode(),
            id="numeric_credentials_in_json",
        ),
        pytest.param(
            ArtifactType.LLM_REQUEST,
            b"\xef\xbb\xbf" + json.dumps({"password": 12345678}).encode(),
            b"\xef\xbb\xbf" + json.dumps({"password": _R}).encode(),
            id="numeric_credential_in_json_after_a_bom",
        ),
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            '\ufeff{"password":12345678}'.encode(),
            f'\ufeff{{"password":"{_R}"}}'.encode(),
            id="numeric_credential_in_json_after_a_bom_in_log",
        ),
        # One element per line of a key: the block crosses strings, so every string it reaches is withheld.
        pytest.param(
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps([{"text": line} for line in _PRIVATE_KEY.split("\n")] + [{"text": "Sign in"}]).encode(),
            json.dumps([{"text": _R} for _ in _PRIVATE_KEY.split("\n")] + [{"text": "Sign in"}]).encode(),
            id="private_key_lines_in_separate_strings",
        ),
        # Thousands of quoted values each running into the next element's string: each string is read on its own, so
        # none is a value, under the time bound below.
        pytest.param(
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps(
                [{"t": "password='"} if index % 2 == 0 else {"t": "abcdefgh'"} for index in range(8000)]
            ).encode(),
            None,
            id="many_spans_crossing_strings",
        ),
        # 256 KB of a key or header with no value after it, or a value that is rejected, under the time bound below.
        *[
            pytest.param(ArtifactType.SKYVERN_LOG, ((shape * (262144 // len(shape))) + end).encode(), None, id=row_id)
            for row_id, shape, end in (
                ("cookie_word_without_a_pair_256kb", "Cookie: x ", ""),
                ("cookie_banner_256kb", "cookie: accept all ", ""),
                ("rejected_query_values_256kb", "password=x?", "x("),
                ("rejected_fragment_values_256kb", "#token=a", "("),
                ("php_variables_in_one_value_256kb", "$password=x", ""),
                ("cookie_values_in_one_run_256kb", "cookie:abcdefgh?", ""),
            )
        ],
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps({"headers": {"Cookie": "sid=Zq9xWv7Lm; JSESSIONID=Zq9xWv7Lm"}}).encode(),
            json.dumps({"headers": {"Cookie": _R}}).encode(),
            id="cookie_header_dict_with_two_cookies",
        ),
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps(
                {
                    "headers": {"Authorization": 'Digest username="u", response="6629fae49393a05397450978507c4ef1"'},
                    "url": "https://x.test/a",
                    "status": 200,
                }
            ).encode(),
            json.dumps(
                {"headers": {"Authorization": f"Digest {_R}"}, "url": "https://x.test/a", "status": 200}
            ).encode(),
            id="digest_header_in_single_line_json",
        ),
        # A log holding a JSON document after more leading whitespace than a fixed prefix would read.
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            (" " * 100 + json.dumps({"password": 83920174})).encode(),
            (" " * 100 + json.dumps({"password": _R})).encode(),
            id="json_after_long_leading_whitespace",
        ),
        pytest.param(
            ArtifactType.SKYVERN_LOG,
            f"ERROR key={_TRUNCATED_PRIVATE_KEY}\n".encode(),
            f"ERROR key={_R}\n".encode(),
            id="truncated_private_key_in_log",
        ),
        pytest.param(
            ArtifactType.LLM_REQUEST,
            json.dumps(
                {
                    "password": "Hunter22xx",
                    "api_key": "abcd" + "1234efgh",
                    "note": 'login password="Abc1,2345;x" ok',
                    "inner": json.dumps({"token": "Zq9xY8wV7u2"}),
                    "key": _TRUNCATED_PRIVATE_KEY,
                    **_JSON_FIELDS,
                }
            ).encode(),
            json.dumps(
                {
                    "password": _R,
                    "api_key": _R,
                    "note": f'login password="{_R}" ok',
                    "inner": json.dumps({"token": _R}),
                    "key": _R,
                    **_JSON_FIELDS,
                }
            ).encode(),
            id="credential_shapes_in_json_keys_and_escaped_quotes",
        ),
        pytest.param(
            ArtifactType.LLM_RESPONSE,
            json.dumps(
                {
                    "a": "a\npassword: Hunter22xx",
                    "b": "a\nBearer abcdef123456789",
                    "c": "a\n" + "sk_" + "live_AbCdEf0123456789XyZw",
                    "d": "a\n" + _API_KEY,
                    "e": "a\ttoken=abcd1234",
                    "f": "a\rsecret=abcd1234",
                    "g": json.dumps({"n": 'login password="Abc1,2345;x" ok'}),
                    "page-token": "abc123x",
                    "path": "C:\\new\\token_dir",
                }
            ).encode(),
            json.dumps(
                {
                    "a": f"a\npassword: {_R}",
                    "b": f"a\nBearer {_R}",
                    "c": f"a\n{_R}",
                    "d": f"a\n{_R}",
                    "e": f"a\ttoken={_R}",
                    "f": f"a\rsecret={_R}",
                    "g": json.dumps({"n": f'login password="{_R}" ok'}),
                    "page-token": "abc123x",
                    "path": "C:\\new\\token_dir",
                }
            ).encode(),
            id="credential_shapes_after_json_escapes",
        ),
        # Too deep for the value walk once a duplicate key sends it to the token pass, so it is scrubbed as text.
        pytest.param(
            ArtifactType.LLM_REQUEST,
            ('{"k": 1, "k": 2, "c": ' + json.dumps("[" * 2000 + '"secret-value"' + "]" * 2000) + "}").encode(),
            None,
            id="deep_text_beside_duplicate_key",
        ),
        # On Python 3.11 only a depth near 800 reaches the token-pass guard; the 2,000-level row covers 3.12 and later.
        pytest.param(
            ArtifactType.LLM_REQUEST,
            ('{"k": 1, "k": 2, "c": ' + json.dumps("[" * 800 + '"secret-value"' + "]" * 800) + "}").encode(),
            None,
            id="text_800_levels_beside_duplicate_key",
        ),
    ],
)
@pytest.mark.parametrize("mask_secrets", [True, False])
@pytest.mark.parametrize(
    "write_path", ["create_artifact", "overwrite_artifact_data", "step_archive", "bundled_step_archive"]
)
async def test_create_artifact_redacts_textual_artifact_data(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    artifact_type: ArtifactType,
    data: bytes,
    expected: bytes | None,
    mask_secrets: bool,
    write_path: str,
) -> None:
    artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts["wr_redact"].mask_secrets = mask_secrets
    monkeypatch.setattr(artifact_manager_module, "_bundling_enabled", lambda: write_path == "bundled_step_archive")
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    # The scan is linear in the data, so every row, the many-crossing-spans one included, stores within this bound.
    started = time.perf_counter()
    if write_path == "create_artifact":
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
        await manager.wait_for_upload_aiotasks([step.task_id])
    elif write_path == "overwrite_artifact_data":
        artifact = await _FakeArtifactDatabase().create_artifact(
            "a_overwrite", artifact_type, "s3://bucket/artifact", organization_id="o_1", workflow_run_id="wr_redact"
        )
        await manager.overwrite_artifact_data(artifact, data)
    else:
        manager._add_to_step_archive(
            manager._get_or_create_step_archive(step, None, None, None), "a", data, artifact_type
        )
        await manager.flush_step_archive(step.step_id)

    assert time.perf_counter() - started < 2.0
    stored = artifact_redaction_setup.stored[0][1]
    if write_path == "bundled_step_archive":
        stored = zipfile.ZipFile(io.BytesIO(stored)).read("a")
    assert stored == (expected or data.replace(b"secret-value", REDACTED_SECRET_PLACEHOLDER.encode()))
    if artifact_type == ArtifactType.LLM_PROMPT:
        _strict_json(stored)


# One of each trigger the scan reads: key words, separators, headers, block scalars, quotes, twins, escapes, URLs,
# input tags, key blocks and JWT starts, plus combinations of them that are slow to scan.
_SCAN_TRIGGER_SHAPES = (
    "password ",
    "id ",
    "password:",
    'api_key="',
    "pwd='abc",
    "pass=>",
    "password%3D",
    "token =&gt; ",
    "$password=",
    "#token=",
    'id="',
    "Authorization: Bearer ",
    'Authorization: Digest a="',
    "Cookie: a=b; ",
    'Cookie: a="',
    'Cookie: a="x y',
    "Cookie: a=x\\/",
    "Set-Cookie: a=",
    "password: |\n",
    "password: |\r",
    "pwd:|\r",
    ",password: | #",
    "password: >\n  ",
    "a:\n  password: |\n    x\n",
    "password: |\\n",
    '"',
    "'''",
    "password_2",
    "_2",
    "_confirm",
    "new_password_confirmation_",
    "\\\\",
    "\\n",
    "\\u0022",
    "&quot;",
    '\\\\\\"',
    "a+b://u:p",
    "<input type=password ",
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    'password=["abcdefgh", ',
    "pwd=[",
    "eyJabcdefghij.",
    "%",
    "</pre>",
    "\n  \\\\\\\"pwd='abc\\\\",
    ':Cookie: a=b; \\\\\\"',
)
# A shape repeated after one key word: a twin name (`password_2_2_..`) is read from its end.
_SCAN_TRIGGER_PREFIXES = {"_2": "password", "_confirm": "password"}
_STRUCTURE_PIECES = (*_SCAN_TRIGGER_SHAPES, "hunter22secret", "Zq9xWv7Lm", "abc=", "a b", _API_KEY, "\n", " ", ",", "'")
_KEPT_JSON_VALUES = {"url": "https://example.test/keep", "status": "ok", "model": "m-1", "note": "Order total 42"}


_LINE_TERMINATORS = ("\n", "\r\n", "\r", "")
# A canary credential in every shape the README lists; none of its bytes may be stored.
_CANARY_HEAD, _CANARY_TAIL = "Cnry7Xq9", "Zt2Lm4wR"
_CANARY = _CANARY_HEAD + _CANARY_TAIL
_CANARY_VALUES = {
    "quoted": f'"{_CANARY}"',
    "bare": _CANARY,
    "array_element": f'["ab", "{_CANARY}"]',
    "block_scalar": "|{t}  " + _CANARY + "{t}  " + _CANARY_TAIL,
}
_CANARY_KEYS = {"bare": "password: ", "quoted": '"password": ', "json_member": '{{"password": '}
_CANARY_SNIPPETS = (
    *[
        key + value + ("}}" if name == "json_member" else "") + "{t}user: admin"
        for name, key in _CANARY_KEYS.items()
        for value_name, value in _CANARY_VALUES.items()
        if not (name == "json_member" and value_name == "block_scalar")
    ],
    '{{"password=' + _CANARY + '": 1}}',
    '{{"https://example.test/a?access_token=' + _CANARY + '": 1}}',
    'Cookie: sid="' + _CANARY_HEAD + "," + _CANARY_TAIL + '", theme=dark{t}',
    'Set-Cookie: sid="' + _CANARY_HEAD + " " + _CANARY_TAIL + '"; Path=/{t}',
    'Cookie: sid="' + _CANARY_HEAD + "\\073" + _CANARY_TAIL + '"{t}',
)
_CANARY_CONTEXTS = {
    "text": lambda text: text,
    "json_string": lambda text: json.dumps({"log": text, "url": "https://example.test/keep"}),
    "json_in_a_json_string": lambda text: json.dumps({"resp": json.dumps({"log": text, "n": 1})}),
    "after_an_odd_quote_and_a_tag": lambda text: 'TV 5" screen <b>x</b>\n' + text,
}


_LINEAR_GROUPS = {"text": 2, "json": 3}
_LINEAR_CHECKS = [f"linear_{mode}_{group}" for mode, groups in _LINEAR_GROUPS.items() for group in range(groups)]
# Shapes a scan may hold an entry for per escape, line or span, beside the scan trigger shapes.
_MEMORY_SHAPES = (
    "password: |\n" + "\n" * 64,
    '\\"' + "\\" * 64,
    "&quot;" + "&amp;" * 64,
    '\\"' + "\\u0041" * 64,
)
_MEMORY_GROUPS = 4
_MEMORY_CHECKS = [f"memory_{mode}_{group}" for mode in ("text", "json") for group in range(_MEMORY_GROUPS)]
# The slowest shapes to scan in JSON mode, each with its slowest line end.
_SLOWEST_JSON_SHAPES = (('Cookie: a="', "\r"), ('api_key="', "\n"), (':Cookie: a=b; \\\\\\"', "\n"))
# Escapes that decode layer after layer: backslashes with one escaped quote, and escape kinds in turn.
_ESCAPE_BUDGET_SHAPES = ('\\"' + "\\" * 64, '\\"&lt;')


def _scan_seconds(data: bytes, *, json_document: bool, repeats: int = 1) -> float:
    # Repeated measures take the best of them with the collector off, so a loaded runner does not read as growth.
    gc.disable()
    try:
        timings = []
        for _ in range(repeats):
            started = time.thread_time()
            redact_credential_shapes_from_bytes(data, json_document=json_document)
            timings.append(time.thread_time() - started)
        return min(timings)
    finally:
        gc.enable()


def _shape_unit(shape: str, end: str) -> str:
    return _SCAN_TRIGGER_PREFIXES.get(shape, "") + (shape.replace("\n", end) + end if end else shape)


def _assert_scan_grows_linearly(unit: str, small_size: int, *, json_document: bool) -> None:
    small_data, large_data = (
        (json.dumps({"v": body}) if json_document else body).encode()
        for body in (unit * (size // len(unit)) for size in (small_size, 4 * small_size))
    )
    small = _scan_seconds(small_data, json_document=json_document)
    large = _scan_seconds(large_data, json_document=json_document)
    if large >= max(8 * small, 0.02):
        small = _scan_seconds(small_data, json_document=json_document, repeats=3)
        large = _scan_seconds(large_data, json_document=json_document, repeats=3)
    # Four times the input takes about four times as long; a quadratic scan takes sixteen.
    assert large < max(8 * small, 0.02), (unit, json_document)


@pytest.mark.parametrize("check", [*_LINEAR_CHECKS, "budget_256kb", *_MEMORY_CHECKS, "json_structure", "canary_recall"])
def test_credential_shape_scan_is_linear_reads_every_canary_and_keeps_json_strings_apart(
    monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    if check.startswith("linear"):
        _, mode, group = check.split("_")
        json_document = mode == "json"
        with monkeypatch.context() as patch:
            # A slow runner must not trip the production budget; growth with size is what is checked.
            patch.setattr(raw_secret_redaction, "SCAN_BUDGET_SECONDS_PER_MB", 60.0)
            for shape in _SCAN_TRIGGER_SHAPES[int(group) :: _LINEAR_GROUPS[mode]]:
                for end in _LINE_TERMINATORS:
                    unit = _shape_unit(shape, end)
                    _assert_scan_grows_linearly(unit, 16384, json_document=json_document)
        return
    if check == "budget_256kb":
        # Block headers after one long line (fewer key words than a budget check interval) scan inside the production
        # budget for their size, checked again once the scan returns.
        padded = "\n" + " " * 250_000 + "password: |\r" * 255
        for data, json_document in ((padded.encode(), False), (json.dumps({"v": padded}).encode(), True)):
            with raw_secret_redaction.scan_budget(len(data)):
                redact_credential_shapes_from_bytes(data, json_document=json_document)
                raw_secret_redaction.check_scan_budget()
        with monkeypatch.context() as patch:
            patch.setattr(raw_secret_redaction, "SCAN_BUDGET_SECONDS_PER_MB", 60.0)
            for shape, end in _SLOWEST_JSON_SHAPES:
                _assert_scan_grows_linearly(_shape_unit(shape, end), 65536, json_document=True)
        for unit in _ESCAPE_BUDGET_SHAPES:
            body = unit * ((1 << 20) // len(unit))
            for data, json_document in ((body.encode(), False), (json.dumps({"v": body}).encode(), True)):
                with raw_secret_redaction.scan_budget(len(data)):
                    redact_credential_shapes_from_bytes(data, json_document=json_document)
                    raw_secret_redaction.check_scan_budget()
        return
    if check.startswith("memory"):
        _, mode, group = check.split("_")
        shapes = (*[_shape_unit(shape, "\n") for shape in _SCAN_TRIGGER_SHAPES], *_MEMORY_SHAPES)
        with monkeypatch.context() as patch:
            # Tracing allocations slows the scan several times over.
            patch.setattr(raw_secret_redaction, "SCAN_BUDGET_SECONDS_PER_MB", 120.0)
            for unit in shapes[int(group) :: _MEMORY_GROUPS]:
                body = unit * ((1 << 20) // len(unit))
                data = (json.dumps({"v": body}) if mode == "json" else body).encode()
                tracemalloc.start()
                try:
                    stored = redact_credential_shapes_from_bytes(data, json_document=mode == "json")
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                # The text, its decoded copy and the stored copy, never an entry per escape, line or span; a placeholder
                # may be longer than the value it replaces, so the bound follows the larger text.
                assert peak < 4.5 * max(len(data), len(stored)), (unit, mode, peak / len(data))
        return
    if check == "canary_recall":
        for snippet in _CANARY_SNIPPETS:
            for end in _LINE_TERMINATORS[:3]:
                for context_name, context in _CANARY_CONTEXTS.items():
                    data = context(snippet.format(t=end)).encode()
                    stored = redact_credential_shapes_from_bytes(data, json_document=data.lstrip()[:1] in (b"{", b"["))
                    assert _CANARY_HEAD.encode() not in stored and _CANARY_TAIL.encode() not in stored, (
                        context_name,
                        data,
                        stored,
                    )
        return
    # Every string is scanned and replaced on its own: the output parses with the same keys, a string with no
    # credential text is kept, and the rest of a scrubbed string is its own text in order.
    rng = random.Random(21942)
    for _ in range(400):
        doc = {
            key: _KEPT_JSON_VALUES.get(key) or "".join(rng.choice(_STRUCTURE_PIECES) for _ in range(rng.randint(1, 10)))
            for key in ("cfg", "url", "a", "status", "b", "model", "note")
        }
        stored = json.loads(redact_credential_shapes_from_bytes(json.dumps(doc).encode(), json_document=True))
        assert list(stored) == list(doc), doc
        for key, value in doc.items():
            if key in _KEPT_JSON_VALUES:
                assert stored[key] == value, doc
                continue
            position = 0
            for fragment in stored[key].split(_R):
                position = value.find(fragment, position)
                assert position >= 0, doc
                position += len(fragment)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data", "failure"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup"),
        (ArtifactType.LLM_REQUEST, b'{"messages": [{"role": "user", "content": "secret-value"}]}', "lookup"),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup",
        ),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup_and_har_scrub",
        ),
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup_with_redaction_switched_off"),
        (ArtifactType.SKYVERN_LOG, _LOG_LINE.encode(), "shape_scrub"),
        (ArtifactType.SKYVERN_LOG, _LOG_LINE.encode(), "shape_scrub_budget"),
    ],
)
async def test_create_artifact_never_stores_raw_data_when_secret_lookup_fails(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    artifact_type: ArtifactType,
    data: bytes,
    failure: str,
) -> None:
    def failing_lookup(*_: object, **__: object) -> set[str]:
        raise RuntimeError("secret store unavailable")

    def failing_scrub(*_: object, **__: object) -> bytes:
        raise RuntimeError("scrub failed")

    if failure == "shape_scrub":
        monkeypatch.setattr(run_data_scrub, "redact_credential_shapes_from_bytes", failing_scrub)
    elif failure == "shape_scrub_budget":
        # A scan past its budget is withheld, so no input shape can hold the event loop.
        monkeypatch.setattr(raw_secret_redaction, "SCAN_BUDGET_BASE_SECONDS", -1.0)
    else:
        monkeypatch.setattr(
            artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "get_secret_values_for_run", failing_lookup
        )
    if failure == "lookup_and_har_scrub":
        monkeypatch.setattr(run_data_scrub, "redact_har_bytes", failing_scrub)
    if failure == "lookup_with_redaction_switched_off":
        # With the switch off the run's opt-in is never read, so the failure has to come from that read.
        monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
        monkeypatch.setattr(
            artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "artifact_redaction_enabled", failing_lookup
        )
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    with capture_logs() as logs:
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
        await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    if stored == REDACTION_FAILED_STUB:
        # A withheld artifact cannot be recovered, so its log line says what was lost and what the scan cost.
        withheld = [log for log in logs if "withholding artifact data" in log["event"]]
        assert withheld and withheld[0]["log_level"] == "warning", logs
        assert withheld[0]["artifact_type"] == artifact_type and withheld[0]["size_bytes"] > 0, withheld
        assert withheld[0]["cpu_seconds"] >= 0, withheld
    if failure == "lookup_with_redaction_switched_off":
        # The kill switch restores the pre-redaction behavior: the artifact is stored as produced.
        assert stored == data
    elif artifact_type == ArtifactType.HAR and failure == "lookup":
        # The HAR keeps its structure; the name-based scrub still removes the credential header value.
        assert b"Bearer token" not in stored
        assert json.loads(stored)["log"]["entries"][0]["request"]["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    else:
        assert stored == REDACTION_FAILED_STUB


@pytest.mark.asyncio
async def test_create_artifact_redacts_registered_bare_task_secret(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    context = SkyvernContext(
        organization_id=TEST_ORGANIZATION_ID,
        task_id=step.task_id,
        workflow_run_id=None,
    )
    context.register_secret_value("482913")
    skyvern_context.set(context)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.LLM_REQUEST,
        data=b'{"role": "tool", "content": "verification_code: 482913"}',
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    artifact, stored = artifact_redaction_setup.stored[0]
    assert artifact.workflow_run_id is None
    assert b"482913" not in stored
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored


@pytest.mark.asyncio
async def test_create_artifact_redacts_using_artifact_workflow_run_id_without_context(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=True,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    assert skyvern_context.current() is None

    manager = ArtifactManager()
    workflow_run_block = create_fake_workflow_run_block("wr_redact", "wrb_redact")

    await manager.create_workflow_run_block_artifact(
        workflow_run_block=workflow_run_block,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])

    assert storage.stored[0][0].workflow_run_id == "wr_redact"
    assert storage.stored[0][1] == f"<html>{REDACTED_SECRET_PLACEHOLDER}</html>".encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data", "switch"),
    [
        (ArtifactType.SCREENSHOT_LLM, b"png secret-value bytes", None),
        # Generated code is read back and run, so no scrub rewrites it.
        (ArtifactType.SCRIPT_FILE, (_CREDENTIALS_PAGE + _LOG_LINE).encode(), None),
        (
            ArtifactType.HTML_SCRAPE,
            b"<html>secret-value</html>" + _CREDENTIALS_PAGE.encode(),
            "ENABLE_SECRET_ARTIFACT_REDACTION",
        ),
        (ArtifactType.HTML_SCRAPE, _CREDENTIALS_PAGE.encode(), "ENABLE_ARTIFACT_CREDENTIAL_SHAPE_SCRUB"),
    ],
)
async def test_create_artifact_leaves_data_unchanged_for_binary_types_and_disabled_switches(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    artifact_type: ArtifactType,
    data: bytes,
    switch: str | None,
) -> None:
    if switch is not None:
        monkeypatch.setattr(artifact_manager_module.settings, switch, False)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == data


def _har_with_session_material(base64_body: bool) -> bytes:
    body = '{"echo": "secret-value", "code": "482913", "n": 94829130, "msg": "code:\\n482913"}'
    content = (
        {"mimeType": "application/json", "encoding": "base64", "text": base64.b64encode(body.encode()).decode()}
        if base64_body
        else {"mimeType": "application/json", "text": body}
    )
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            # The task's runtime code touches other digits in the URL, a JSON number and a body.
                            "url": "https://example.test/login?ref=7482913",
                            "headers": [
                                {"name": "Cookie", "value": "sid=cookie-value-1"},
                                {"name": "Authorization", "value": "Bearer bearer-value-1"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-1"}],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "text": "",
                                "params": [
                                    {"name": "username", "value": "someone"},
                                    {"name": "password", "value": "form-password-1"},
                                ],
                            },
                        },
                        "response": {
                            "headers": [
                                {"name": "Set-Cookie", "value": "sid=cookie-value-2; Path=/"},
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-2"}],
                            "bodySize": 4829135,
                            "content": content,
                        },
                    }
                ]
            }
        }
    ).encode()


async def _check_opted_out_run_secrets_only_under_all_runs_switch(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    scrub_all_runs: bool | None,
) -> None:
    # None runs on the shipped default, which is on.
    if scrub_all_runs is not None:
        monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS", scrub_all_runs)
    context_manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact",
        mask_secrets=False,
        secrets={
            "password": "secret-value",
            "username": "admin",
            "code": "12345678",
            "quoted": 'quo"ted-pass',
            "doc": '{"demo": "fixture-only"}',
            "spanning": 'alpha", "bravo',
        },
    )
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", context_manager)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    html = b"<html>secret-value admin</html>"
    har = json.dumps({"log": {"entries": [{"response": {"content": {"text": "secret-value"}}}]}}).encode()
    # A model reply whose message content is itself JSON text, holding a number-valued and a quoted secret.
    reply = json.dumps({"reference": 12345678, "answer": 'quo"ted-pass'})
    llm = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
    # A duplicate key would drop the earlier copy in a parse; a long float would round its digits away.
    request = b'{"n": 123456789012345678901.5}'
    duplicated = b'{"answer": "secret-value", "answer": "x"}'
    # The quoted secret is escaped twice in the raw bytes, so only a decoded read finds it.
    rendered = json.dumps({"content": json.dumps({"answer": 'quo"ted-pass'})}).encode()
    # A registered secret that is itself a JSON document, shown as an element's text.
    tree = json.dumps([{"id": "a", "text": '{"demo": "fixture-only"}'}]).encode()
    # Each JSON case lists the stored value with every JSON text layer decoded, and must stay parseable.
    json_cases = [
        (
            ArtifactType.LLM_RESPONSE,
            json.dumps({"choices": [{"message": {"content": json.dumps({"reference": 12345678})}}]}),
            {"choices": [{"message": {"content": {"reference": REDACTED_SECRET_PLACEHOLDER}}}]},
        ),
        (ArtifactType.LLM_RESPONSE_PARSED, "12345678", REDACTED_SECRET_PLACEHOLDER),
        (ArtifactType.LLM_RESPONSE_PARSED, json.dumps("secret-value"), REDACTED_SECRET_PLACEHOLDER),
        (ArtifactType.LLM_RESPONSE_PARSED, '{"demo": "fixture-only"}', REDACTED_SECRET_PLACEHOLDER),
        (
            ArtifactType.LLM_RESPONSE_PARSED,
            '{"payload": {"demo": "fixture-only"}}',
            {"payload": REDACTED_SECRET_PLACEHOLDER},
        ),
        (ArtifactType.LLM_RESPONSE_PARSED, '["alpha", "bravo"]', [REDACTED_SECRET_PLACEHOLDER]),
        (
            ArtifactType.LLM_RESPONSE_RENDERED,
            json.dumps({"c": json.dumps({"d": {"demo": "fixture-only"}, "p": "secret-value"})}),
            {"c": {"d": REDACTED_SECRET_PLACEHOLDER, "p": REDACTED_SECRET_PLACEHOLDER}},
        ),
        # A document-valued secret written in another formatting, as JSON text with no escapes in it.
        (
            ArtifactType.LLM_RESPONSE_RENDERED,
            json.dumps({"c": json.dumps({"k": {"demo": "fixture-only"}}, separators=(",", ":"))}),
            {"c": {"k": REDACTED_SECRET_PLACEHOLDER}},
        ),
        # Half of a split emoji cannot be written as UTF-8 unless it is escaped.
        (
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"t": "\ud83d", "n": 12345678, "secret-value": 1}),
            {"t": "\ud83d", "n": REDACTED_SECRET_PLACEHOLDER, REDACTED_SECRET_PLACEHOLDER: 1},
        ),
        # Past the double range, so it parses as inf and cannot be written back as standard JSON.
        (
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"big": 1e400, "p": "secret-value"}',
            {"big": float("inf"), "p": REDACTED_SECRET_PLACEHOLDER},
        ),
    ]
    # Each runs on its own secrets, so the spelling screen decides whether the artifact is parsed.
    field = json.dumps({"answer": 'quo"ted-pass'})
    quoted = {"password": "secret-value", "quoted": 'quo"ted-pass'}
    numeric_document = {"a": 1234, "b": 5678}
    spanning = {"password": "secret-value", "spanning": 'alpha", "bravo'}
    screened_cases = [
        # A form field's JSON text inside tool arguments inside the request: three escaping layers.
        (
            quoted,
            ArtifactType.LLM_REQUEST,
            json.dumps({"messages": [{"content": json.dumps({"text": field})}]}),
            {"messages": [{"content": {"text": {"answer": REDACTED_SECRET_PLACEHOLDER}}}]},
        ),
        # Four layers, so the secret is escaped too often for a text match before the walk decodes it.
        (
            quoted,
            ArtifactType.LLM_REQUEST,
            json.dumps({"a": json.dumps({"b": json.dumps({"c": json.dumps({"d": field})})})}),
            {"a": {"b": {"c": {"d": {"answer": REDACTED_SECRET_PLACEHOLDER}}}}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": '{"p": "s\\u0065cret-value"}'}),
            {"c": {"p": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (quoted, ArtifactType.VISIBLE_ELEMENTS_TREE, '{"p": "secret-va\\u006Cue"}', {"p": REDACTED_SECRET_PLACEHOLDER}),
        (quoted, ArtifactType.VISIBLE_ELEMENTS_TREE, '{"p": "\\u0073ecret-value"}', {"p": REDACTED_SECRET_PLACEHOLDER}),
        # Text that cannot round-trip through a parse still has its nested JSON layers scrubbed.
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k": 1, "k": 2, "c": ' + json.dumps(field) + "}",
            {"k": 2, "c": {"answer": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"big": 1e400, "c": ' + json.dumps(field) + "}",
            {"big": float("inf"), "c": {"answer": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": '{"k": 1, "k": 2, "q": ' + json.dumps(field) + "}"}),
            {"c": {"k": 2, "q": {"answer": REDACTED_SECRET_PLACEHOLDER}}},
        ),
        (
            {"code": "12345678"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"n": 1.2345678e7, "s": "12345678"}',
            {"n": REDACTED_SECRET_PLACEHOLDER, "s": REDACTED_SECRET_PLACEHOLDER},
        ),
        (
            {"code": "12345678"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k":1,"k":2,"n":12345678}',
            {"k": 2, "n": REDACTED_SECRET_PLACEHOLDER},
        ),
        (
            spanning,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k": 1, "k": 2, "l": ["alpha", "bravo"]}',
            {"k": 2, "l": [REDACTED_SECRET_PLACEHOLDER]},
        ),
        (
            {**spanning, **quoted},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"l": ["alpha", "bravo"], "c": field}),
            {"l": [REDACTED_SECRET_PLACEHOLDER], "c": {"answer": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            {"path": "a/b/secret-path"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"p": "a\\/b\\/secret-path"}',
            {"p": REDACTED_SECRET_PLACEHOLDER},
        ),
        # No character of this secret is printable ASCII, so no literal run can screen it out.
        (
            {"name": "ÄÖÜßäöüé"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"p": "ÄÖÜßäöüé"}, ensure_ascii=False),
            {"p": REDACTED_SECRET_PLACEHOLDER},
        ),
        # A match spanning keys and values inside nested JSON text is found by rescanning that text.
        (
            spanning,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": json.dumps({"d": json.dumps({"p": "secret-value", "l": ["alpha", "bravo"]})})}),
            {"c": {"d": {"p": REDACTED_SECRET_PLACEHOLDER, "l": [REDACTED_SECRET_PLACEHOLDER]}}},
        ),
        # A document secret is found by its keys and string leaves in any formatting; one with neither
        # is always parsed.
        *[
            (
                {"doc": json.dumps(document)},
                ArtifactType.LLM_RESPONSE_PARSED,
                json.dumps({"x": document}, indent=indent, separators=separators),
                {"x": REDACTED_SECRET_PLACEHOLDER},
            )
            for document in (numeric_document, [1234, 5678])
            for indent, separators in ((None, (",", ":")), (2, None))
        ],
        # A registered number inside a document secret does not keep the whole document from matching.
        (
            {"code": "12345678", "doc": '{"user": "svc-user-1", "ref": 12345678}'},
            ArtifactType.LLM_RESPONSE_PARSED,
            json.dumps({"cfg": {"user": "svc-user-1", "ref": 12345678}}),
            {"cfg": REDACTED_SECRET_PLACEHOLDER},
        ),
    ]
    # The registered number touches other digits in a URL, HAR numbers and bodies, with and without a base64
    # body; in the third HAR only the numbers hold it, and in the last its digits only follow a JSON escape.
    har_digit_cases = [
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {"url": url},
                            "response": {"bodySize": 1123456789, "content": {"size": 12345678, **content}},
                        }
                    ]
                }
            }
        ).encode()
        for url, content in (
            ("https://example.test/a?q=912345678", {"text": "ref=0123456789"}),
            (
                "https://example.test/a?q=912345678",
                {"encoding": "base64", "text": base64.b64encode(b"ref=0123456789").decode()},
            ),
            ("https://example.test/a", {"text": "plain"}),
            ("https://example.test/a", {"text": "\u54812345678"}),
        )
    ]
    # A document secret keeps the screen: JSON holding none of the run's secrets is returned without a parse.
    unparsed = b'{"k": "x", "k": "y"}'
    assert redact_secrets_from_json_bytes(unparsed, {"secret-value", '{"demo": "fixture-only"}'}) is unparsed

    await manager.create_artifact(step=step, artifact_type=ArtifactType.HTML_SCRAPE, data=html)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE, data=llm)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_REQUEST, data=request)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE_PARSED, data=duplicated)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE_RENDERED, data=rendered)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE, data=tree)
    for artifact_type, data, _ in json_cases:
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data.encode())
    await manager.wait_for_upload_aiotasks([step.task_id])
    for har_case in har_digit_cases:
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_case)
    await manager.wait_for_upload_aiotasks([step.task_id])
    for secrets, artifact_type, data, _ in screened_cases:
        screened_context = workflow_context_manager_factory(
            workflow_run_id="wr_redact", mask_secrets=False, secrets=secrets
        )
        monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", screened_context)
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data.encode())
        await manager.wait_for_upload_aiotasks([step.task_id])

    har_digit_end = 7 + len(json_cases) + len(har_digit_cases)
    har_digit_stored = [data for _, data in artifact_redaction_setup.stored[7 + len(json_cases) : har_digit_end]]
    json_stored = [
        data
        for _, data in artifact_redaction_setup.stored[7 : 7 + len(json_cases)]
        + artifact_redaction_setup.stored[har_digit_end:]
    ]
    stored = {artifact.artifact_type: data for artifact, data in artifact_redaction_setup.stored[:7]}
    if scrub_all_runs is not False:
        # A short value matches on word boundaries and would rewrite ordinary text, so it is kept.
        assert stored[ArtifactType.HTML_SCRAPE] == f"<html>{REDACTED_SECRET_PLACEHOLDER} admin</html>".encode()
        assert b"secret-value" not in stored[ArtifactType.HAR]
        content = json.loads(stored[ArtifactType.LLM_RESPONSE])["choices"][0]["message"]["content"]
        assert json.loads(content) == {
            "reference": REDACTED_SECRET_PLACEHOLDER,
            "answer": REDACTED_SECRET_PLACEHOLDER,
        }
        assert json.loads(stored[ArtifactType.LLM_REQUEST]) == {"n": REDACTED_SECRET_PLACEHOLDER}
        assert b"secret-value" not in stored[ArtifactType.LLM_RESPONSE_PARSED]
        assert stored[ArtifactType.LLM_RESPONSE_PARSED].count(b'"answer"') == 2
        rendered_content = json.loads(json.loads(stored[ArtifactType.LLM_RESPONSE_RENDERED])["content"])
        assert rendered_content == {"answer": REDACTED_SECRET_PLACEHOLDER}
        assert json.loads(stored[ArtifactType.VISIBLE_ELEMENTS_TREE]) == [
            {"id": "a", "text": REDACTED_SECRET_PLACEHOLDER}
        ]
        assert [_decode_json_text_layers(_strict_json(data)) for data in json_stored] == [
            *[expected for _, _, expected in json_cases],
            *[expected for *_, expected in screened_cases],
        ]
        for har_stored in har_digit_stored:
            har_entry = _strict_json(har_stored)
            assert isinstance(har_entry, dict)
            response = har_entry["log"]["entries"][0]["response"]
            har_content = response["content"]
            har_body = (
                base64.b64decode(har_content["text"]).decode() if "encoding" in har_content else har_content["text"]
            )
            assert response["bodySize"] == har_content["size"] == REDACTED_SECRET_PLACEHOLDER
            assert "12345678" not in json.dumps(har_entry, ensure_ascii=False)
            assert har_body in (f"ref=0{REDACTED_SECRET_PLACEHOLDER}9", "plain", "\u54812345678")
    else:
        assert har_digit_stored == har_digit_cases
        assert stored == {
            ArtifactType.HTML_SCRAPE: html,
            ArtifactType.HAR: har,
            ArtifactType.LLM_RESPONSE: llm,
            ArtifactType.LLM_REQUEST: request,
            ArtifactType.LLM_RESPONSE_PARSED: duplicated,
            ArtifactType.LLM_RESPONSE_RENDERED: rendered,
            ArtifactType.VISIBLE_ELEMENTS_TREE: tree,
        }
        assert json_stored == [
            *[data.encode() for _, data, _ in json_cases],
            *[data.encode() for _, _, data, _ in screened_cases],
        ]
    # The prompt and row scrubs read the per-workflow opt-in, which stays off.
    assert context_manager.get_secret_values_for_run("wr_redact") == set()


async def _check_har_structured_fields_whether_or_not_workflow_opted_in(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    mask_secrets: bool,
    lookup_fails: bool,
    source: str,
    base64_body: bool,
) -> None:
    context_manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact", mask_secrets=mask_secrets, secrets={"password": "secret-value"}
    )
    if lookup_fails:

        def failing_lookup(*_: object, **__: object) -> set[str]:
            raise RuntimeError("secret store unavailable")

        monkeypatch.setattr(context_manager, "get_secret_values_for_run", failing_lookup)
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", context_manager)
    runtime_context = skyvern_context.current()
    assert runtime_context is not None
    runtime_context.runtime_secret_values.add("482913")
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    har_path = tmp_path / "run.har"
    har_path.write_bytes(_har_with_session_material(base64_body))

    if source == "data":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_path.read_bytes())
    elif source == "path":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, path=str(har_path))
    else:
        artifact_model = ArtifactModel(
            artifact_id="a_har_bulk",
            artifact_type=ArtifactType.HAR,
            uri="s3://bucket/artifact",
            organization_id=TEST_ORGANIZATION_ID,
            task_id=step.task_id,
            step_id=step.step_id,
            workflow_run_id="wr_redact",
        )
        await manager.bulk_create_artifacts(
            [
                BulkArtifactCreationRequest(
                    artifacts=[ArtifactBatchData(artifact_model=artifact_model, path=str(har_path))],
                    primary_key=step.task_id,
                )
            ]
        )
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    entry = json.loads(stored)["log"]["entries"][0]
    if base64_body:
        stored += base64.b64decode(entry["response"]["content"]["text"])
    for leaked in (b"cookie-value-1", b"cookie-value-2", b"bearer-value-1", b"form-password-1"):
        assert leaked not in stored
    # Antibot detection reads cookie names and the submission verifier reads content-type.
    assert [cookie["name"] for cookie in entry["request"]["cookies"]] == ["sid"]
    assert [cookie["name"] for cookie in entry["response"]["cookies"]] == ["sid"]
    assert {"name": "Content-Type", "value": "application/json"} in entry["response"]["headers"]
    assert {"name": "username", "value": "someone"} in entry["request"]["postData"]["params"]
    # A run whose secret lookup succeeded also scrubs its credential values from bodies; an opted-out run
    # does so under the all-runs switch, which is on by default.
    assert (b"secret-value" in stored) is lookup_fails
    # The one-time code comes from the task, so only the opted-in lookup's failure loses it.
    assert (b"482913" in stored) is (mask_secrets and lookup_fails)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        *[("all_runs_switch", scrub_all_runs) for scrub_all_runs in (None, True, False)],
        *[
            ("har_fields", mask_secrets, lookup_fails, source, base64_body)
            for mask_secrets, lookup_fails in ((True, False), (False, False), (True, True), (False, True))
            for source in ("data", "path", "bulk_path")
            for base64_body in (False, True)
        ],
    ],
    ids=str,
)
async def test_create_artifact_scrubs_har_fields_and_opted_out_run_secrets(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    case: tuple[str | bool | None, ...],
) -> None:
    if case[0] == "all_runs_switch":
        await _check_opted_out_run_secrets_only_under_all_runs_switch(
            artifact_redaction_setup, monkeypatch, workflow_context_manager_factory, *case[1:]
        )
    else:
        await _check_har_structured_fields_whether_or_not_workflow_opted_in(
            artifact_redaction_setup, monkeypatch, tmp_path, workflow_context_manager_factory, *case[1:]
        )
