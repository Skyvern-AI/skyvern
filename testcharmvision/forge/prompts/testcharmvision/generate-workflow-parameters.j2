You are an expert at analyzing user interface automation actions and generating meaningful field names for data structures.

Given a list of input_text, upload_file and select_option actions with their intentions and values, generate appropriate field names for a Pydantic BaseModel class called "GeneratedWorkflowParameters".

## Rules:
1. Field names should be valid Python identifiers (snake_case, no spaces, no special characters except underscore)
2. Field names should be descriptive and based on the intention of the action
3. If multiple actions use the same text value, they should map to the same field name
4. Field names should be concise but clear about what data they represent
5. Avoid generic names like "field1", "input1" - use meaningful names based on the intention
{% if has_existing_fields %}
6. CRITICAL: Some actions have existing field names that MUST be preserved exactly as specified. These field names are used by cached code and changing them will break the workflow. You MUST use the exact existing field name for these actions.
{% endif %}

## Actions:
{% for action in custom_field_actions %}
Action {{ loop.index }}:
- Action type: "{{ action.action_type }}"
- Value: "{{ action.value }}"
- Intention: "{{ action.intention }}"
{% if action.existing_field_name %}- EXISTING FIELD NAME (MUST PRESERVE): "{{ action.existing_field_name }}"
{% endif %}
{% endfor %}

## Expected Output:
Return a JSON object with the following structure:
```json
{
  "field_mappings": {
    "action_index_1": "field_name_1",
    "action_index_2": "field_name_2",
    ...
  },
  "schema_fields": {
    "field_name_1": {
      "type": "str",
      "description": "Description of what this field represents"
    },
    "field_name_2": {
      "type": "str",
      "description": "Description of what this field represents"
    },
    ...
  }
}
```

Where:
- `field_mappings` maps each action index (1-based) to its corresponding field name
- `schema_fields` defines each unique field with its type and description
- Actions with the same text value should map to the same field name
{% if has_existing_fields %}- Actions with an existing field name MUST use that exact field name in both `field_mappings` and `schema_fields`
{% endif %}

Generate the field names now:
