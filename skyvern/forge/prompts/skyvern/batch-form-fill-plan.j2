# Goal
Plan values for form fields that need filling.

# User's Goal
{{ goal }}

# Available Data
{% if data and data is mapping and data.items()|list|length > 0 %}
{% for key, value in data.items() %}
- {{ key }}: {{ value }}
{% endfor %}
{% elif data %}
{{ data }}
{% endif %}

# Form Fields
{% for field in fields %}
{{ loop.index }}. **{{ field.label }}** ({{ field.format_hint }})
{%- if field.required %} [REQUIRED]{% endif %}
{%- if field.placeholder %} — hint: "{{ field.placeholder }}"{% endif %}
{%- if field.options %} — options: {{ field.options | join(", ") }}{% endif %}
{% endfor %}

# Rules
## CRITICAL: You MUST provide a value for EVERY field unless noted below.
- Return null ONLY for file upload fields or truly optional fields where no matching data exists in the context.
- For choice fields (radio, dropdown, checkbox) with listed options: you MUST select an option. NEVER return null.
- If no data matches a choice field, pick the most reasonable/common/default option.
- If no data matches a text field, make a reasonable professional guess based on the context.

## CRITICAL for choice fields (radio, dropdown, checkbox):
- Return the EXACT option label text as listed in the options above. Do NOT rephrase or summarize.
- Example: if options are "I am not a protected veteran", "I am a protected veteran" — return "I am not a protected veteran", NOT "No".
- Example: if options are "Man", "Woman", "Non-binary", "Prefer not to say" — return "Man", NOT "Male".
- Example: if options are "Decline to self-identify", "Yes, I have a disability" — return the exact text, NOT "No" or "Decline".
- For checkboxes: return a JSON array of exact labels to check.

## Format rules by field type
- **Short text**: brief factual value (1-5 words). NEVER sentences or paragraphs.
- **Email/URL/phone**: exact formatted value.
- **Paragraph/essay**: 2-3 professional sentences using the available data context.
- **Dropdown select**: pick EXACTLY ONE option from the listed options. Return the EXACT option label text as a string. NEVER return null.
- **Single choice radio**: pick EXACTLY ONE option from the listed options. Return the EXACT option label. NEVER return null.
- **Multi-select checkboxes**: return a JSON array of EXACT selected option labels. Pick ONLY relevant ones, NOT all. Select at least one if required.
- **Single checkbox**: return true or false. NEVER return null.
- **File upload**: return null (file uploads are handled separately).

# Output Format
JSON object mapping field number (string) to value. Return null only for file uploads or truly optional fields with no matching data:
{"1": "2020", "2": "Other", "3": ["English", "Spanish"], "4": "A brief professional response.", "5": null}
