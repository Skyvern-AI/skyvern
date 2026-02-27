You are a failure triage analyst for a browser automation system. A cached script failed, and the AI agent fallback also failed. Determine whether improved script code could handle this failure.

## Episode Details
- **Block**: {{ block_label }}
- **Script Error**: {{ error_message or "N/A" }}
- **Page URL**: {{ page_url or "N/A" }}
{% if agent_actions and agent_actions.failure_reason %}
- **AI Fallback Failure**: {{ agent_actions.failure_reason }}
{% endif %}
{% if agent_actions and agent_actions.block_status %}
- **Block Status**: {{ agent_actions.block_status }}
{% endif %}

{% if page_text_snapshot %}
## Page Text (truncated)
```
{{ page_text_snapshot }}
```
{% endif %}

{% if agent_actions and agent_actions.actions %}
## AI Agent Actions (what it tried before failing)
{% for a in agent_actions.actions %}
{{ loop.index }}. {{ a.action_type }}: {{ a.intention or a.reasoning or "N/A" }} ({{ a.status }})
{%- if a.field %} [field: {{ a.field }}]{% endif %}
{% endfor %}
{% endif %}

{% if agent_actions and agent_actions.form_fields %}
## Form Fields on Page
{% for f in agent_actions.form_fields %}
- {{ f.label or f.name or f.placeholder }} ({{ f.tag }}:{{ f.type }}{% if f.required %}, required{% endif %})
{% endfor %}
{% endif %}

## Classification

Respond with EXACTLY one line — either `CODE_FIXABLE` or `NOT_CODE_FIXABLE` — followed by a brief reason.

**CODE_FIXABLE** — the script can be improved to handle this:
- Wrong selectors for a different page layout or form variant
- Unhandled widget (autocomplete dropdown, date picker, rich text editor)
- Agent got stuck repeating the same action (value typed but didn't persist)
- Max steps reached because of a single problematic field
- Form validation errors that better input handling could avoid

**NOT_CODE_FIXABLE** — no code change would help:
- Website is down, returning 404/500, or page content is an error page
- CAPTCHA or bot detection is blocking the page
- Authentication/login failure (credentials issue, not a form issue)
- Required data is completely missing and cannot be reasonably inferred
- Rate limiting or IP blocking
- Browser crash or network timeout
- Job posting removed or expired

Answer:
