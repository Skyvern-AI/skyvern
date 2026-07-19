"""Regression tests: Jinja reference rewrites in sanitize_workflow_yaml_with_references must not chain.

When two identifiers sanitize into a collision (e.g. "user email" -> user_email, forcing the
pre-existing user_email to user_email_2), the reference-rewrite passes used to apply the renames
one at a time, so the second substitution re-hit the first one's output and a repaired reference
ended up pointing at the wrong parameter or block output. The error_code_mapping pass already
guarded against this; these tests cover the same guarantee for blocks, parameters, and the
workflow_system_prompt.
"""

from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references


def test_colliding_param_renames_do_not_chain_in_block_fields() -> None:
    # "user email" -> user_email takes the name; the valid "user_email" is suffixed to user_email_2.
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "user email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "user_email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
            ],
            "blocks": [
                {
                    "label": "send",
                    "block_type": "task",
                    "url": "https://example.com",
                    "navigation_goal": "Send to {{ user email }} and CC {{ user_email }}",
                }
            ],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    goal = sanitized["workflow_definition"]["blocks"][0]["navigation_goal"]
    # {{ user email }} must land on user_email (not chain on to user_email_2),
    # while {{ user_email }} must follow its parameter to user_email_2.
    assert goal == "Send to {{ user_email }} and CC {{ user_email_2 }}"


def test_colliding_label_renames_do_not_chain_output_refs_in_block_fields() -> None:
    # foo/bar -> foo_bar takes the name; the valid foo_bar label is suffixed to foo_bar_2.
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {"label": "foo/bar", "block_type": "task", "url": "https://example.com"},
                {"label": "foo_bar", "block_type": "task", "url": "https://example.com"},
                {
                    "label": "summarize",
                    "block_type": "task",
                    "url": "https://example.com",
                    "navigation_goal": "first {{ foo/bar_output }}, second {{ foo_bar_output }}, short {{ foo_bar }}",
                },
            ],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    goal = sanitized["workflow_definition"]["blocks"][2]["navigation_goal"]
    # {{ foo/bar_output }} must land on foo_bar_output (not chain on to foo_bar_2_output),
    # while the references to the original foo_bar block must follow it to foo_bar_2.
    assert goal == "first {{ foo_bar_output }}, second {{ foo_bar_2_output }}, short {{ foo_bar_2 }}"


def test_colliding_param_renames_do_not_chain_in_system_prompt_and_param_defaults() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "user email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "user_email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {
                    "key": "greeting",
                    "parameter_type": "workflow",
                    "workflow_parameter_type": "string",
                    "default_value": "Hello {{ user email }}",
                },
            ],
            "blocks": [{"label": "send", "block_type": "task", "url": "https://example.com"}],
            "workflow_system_prompt": "Prefer {{ user email }} over {{ user_email }}",
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    definition = sanitized["workflow_definition"]
    assert definition["workflow_system_prompt"] == "Prefer {{ user_email }} over {{ user_email_2 }}"
    greeting = next(p for p in definition["parameters"] if p["key"] == "greeting")
    assert greeting["default_value"] == "Hello {{ user_email }}"


def test_colliding_param_rename_is_consistent_across_keys_source_refs_and_jinja() -> None:
    # End-to-end composition guard: a single collision must resolve consistently across all three
    # surfaces at once -- the final parameter keys, direct string references (source_parameter_key),
    # and Jinja references. The Jinja pass (this PR) and the direct-string parameter-ref pass (the
    # parameter-key-uniqueness fix this stacks on) each fix one surface; only the combined result is
    # a valid workflow. Previously the direct-string pass chained the collision rename back onto the
    # already-renamed key field, producing duplicate keys and a source_parameter_key with no backing
    # parameter even though the Jinja assertions passed.
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "user email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "user_email", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "cc", "parameter_type": "context", "source_parameter_key": "user_email"},
            ],
            "blocks": [
                {
                    "label": "send",
                    "block_type": "task",
                    "url": "https://example.com",
                    "navigation_goal": "To {{ user_email }} and CC {{ user email }}",
                }
            ],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    definition = sanitized["workflow_definition"]
    parameters = definition["parameters"]

    # Final parameter keys stay distinct: neither param is clobbered into the other's name.
    assert [p["key"] for p in parameters] == ["user_email", "user_email_2", "cc"]
    # source_parameter_key followed its target parameter (original user_email -> user_email_2).
    cc = next(p for p in parameters if p["key"] == "cc")
    assert cc["source_parameter_key"] == "user_email_2"
    # Jinja: {{ user_email }} follows its parameter to user_email_2; {{ user email }} lands on
    # user_email without chaining on to user_email_2.
    assert definition["blocks"][0]["navigation_goal"] == "To {{ user_email_2 }} and CC {{ user_email }}"


def test_non_colliding_renames_still_rewritten_and_unrelated_refs_untouched() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "my-key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
            ],
            "blocks": [
                {
                    "label": "send",
                    "block_type": "task",
                    "url": "https://example.com",
                    "navigation_goal": "use {{ my-key }} but not {{ my_key_lookalike }} or {{ other }}",
                }
            ],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    goal = sanitized["workflow_definition"]["blocks"][0]["navigation_goal"]
    assert goal == "use {{ my_key }} but not {{ my_key_lookalike }} or {{ other }}"
