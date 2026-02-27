# Form Fill Quality Audit

You are auditing a form that was automatically filled by a bot. Your job is to check whether the values entered are correct and in the right fields.

# Applicant Data (source of truth)
{{ data }}

# Form Fields After Auto-Fill
{% for field in fields %}
{{ loop.index }}. **{{ field.label }}** (type: {{ field.type }})
   Value: {{ field.value if field.value is not none and field.value != '' else "(empty)" }}
{% endfor %}

# Instructions
For each field, evaluate:
1. Is the value correct based on the applicant data?
2. Is the value in the right field (not scrambled with another field)?
3. Is the format appropriate (e.g., phone numbers look like phone numbers)?
4. For choice fields (radio/dropdown): was a reasonable option selected?

# Output Format
Return a JSON object:
{
  "score": <number 0-100>,
  "field_count": <number of fields audited>,
  "correct_count": <number of fields filled correctly>,
  "issues": [
    {"field_number": 1, "field_label": "...", "expected": "...", "actual": "...", "severity": "critical|warning|minor", "problem": "..."}
  ],
  "summary": "<one-line overall assessment>"
}

Severity levels:
- **critical**: wrong value in wrong field (scrambling), completely wrong data
- **warning**: reasonable but imprecise value, suboptimal choice selection
- **minor**: formatting issue, slightly different phrasing but correct intent

If all fields look correct, return score=100, empty issues, and a positive summary.
