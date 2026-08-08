import json

from dev_scripts.replay_copilot_turn import _drop_fields_from_tool_outputs, _load_extra_tools


def test_drop_fields_from_tool_outputs_removes_nested_json_fields_only() -> None:
    items = [
        {
            "type": "function_call_output",
            "output": [
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {
                            "ok": True,
                            "resolved_login_credential_id": "cred_123",
                            "nested": {
                                "resolved_login_credential_name": "named-login",
                                "keep": "value",
                            },
                        }
                    ),
                }
            ],
        },
        {
            "type": "function_call_output",
            "output": json.dumps(
                {
                    "resolved_login_page_url": "https://example.com/login",
                    "keep": "second",
                }
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"resolved_login_credential_id": "keep-outside-tool-output"}),
        },
    ]

    cleaned, hits = _drop_fields_from_tool_outputs(
        items,
        {
            "resolved_login_credential_id",
            "resolved_login_credential_name",
            "resolved_login_page_url",
        },
    )

    assert hits == 3
    first = json.loads(cleaned[0]["output"][0]["text"])
    second = json.loads(cleaned[1]["output"])
    assert first == {"ok": True, "nested": {"keep": "value"}}
    assert second == {"keep": "second"}
    assert "keep-outside-tool-output" in cleaned[2]["content"]


def test_load_extra_tools_accepts_function_schema(tmp_path) -> None:
    schema_path = tmp_path / "resolve_credential_reference.json"
    schema_path.write_text(
        json.dumps(
            {
                "name": "resolve_credential_reference",
                "description": "Resolve an exact saved credential reference.",
                "parameters": {
                    "type": "object",
                    "properties": {"reference": {"type": "string"}},
                    "required": ["reference"],
                    "additionalProperties": False,
                },
            }
        )
    )

    tools = _load_extra_tools([schema_path])

    assert [tool.name for tool in tools] == ["resolve_credential_reference"]
    assert tools[0].params_json_schema["required"] == ["reference"]
