# Goal
Extract structured data from input text into canonical form field categories.

# Input Text
{{ input_text }}

# Categories to Extract
For each category below, extract the matching value from the input text. Return null if not found.

{% for cat in categories %}
{{ loop.index }}. **{{ cat.name }}** — {{ cat.prompt }}
{% endfor %}

# Rules
- Extract EXACT values from the context — do NOT invent or hallucinate data.
- For name, email, phone, URLs: extract the literal value.
- For location: extract as "City, State" or "City, Country" format.
- For work authorization, gender, race, veteran, disability: extract the stated answer or preference, or null if not mentioned.
- For salary: extract the number or range as stated.
- For cover letter / additional info: return null (these require generation, not extraction).
- For referral source: return null unless explicitly stated.
- If the context is ambiguous for a field, return null rather than guessing.

# Output Format
JSON object mapping category name to extracted value (or null):
```json
{
  "full_name": "John Doe",
  "email": "john@example.com",
  "phone": null,
  ...
}
```
