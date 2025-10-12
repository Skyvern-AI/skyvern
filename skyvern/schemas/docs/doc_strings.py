TASK_PROMPT_DOC_STRING = """
The goal or task description for Skyvern to accomplish
"""

TASK_URL_DOC_STRING = """
The starting URL for the task. If not provided, Skyvern will attempt to determine an appropriate URL
"""

TASK_ENGINE_DOC_STRING = """
The engine that powers the agent task. The default value is `skyvern-2.0`, the latest Skyvern agent that performs pretty well with complex and multi-step tasks. `skyvern-1.0` is good for simple tasks like filling a form, or searching for information on Google. The `openai-cua` engine uses OpenAI's CUA model. The `anthropic-cua` uses Anthropic's Claude Sonnet 3.7 model with the computer use tool.
"""

PROXY_LOCATION_DOC_STRING = """
Geographic Proxy location to route the browser traffic through. This is only available in Skyvern Cloud.

Available geotargeting options:
- RESIDENTIAL: the default value. Skyvern Cloud uses a random US residential proxy.
- RESIDENTIAL_ES: Spain
- RESIDENTIAL_IE: Ireland
- RESIDENTIAL_GB: United Kingdom
- RESIDENTIAL_IN: India
- RESIDENTIAL_JP: Japan
- RESIDENTIAL_FR: France
- RESIDENTIAL_DE: Germany
- RESIDENTIAL_NZ: New Zealand
- RESIDENTIAL_ZA: South Africa
- RESIDENTIAL_AR: Argentina
- RESIDENTIAL_AU: Australia
- RESIDENTIAL_ISP: ISP proxy
- US-CA: California
- US-NY: New York
- US-TX: Texas
- US-FL: Florida
- US-WA: Washington
- NONE: No proxy
"""

DATA_EXTRACTION_SCHEMA_DOC_STRING = """
The schema for data to be extracted from the webpage. If you're looking for consistent data schema being returned by the agent, it's highly recommended to use https://json-schema.org/.
"""

ERROR_CODE_MAPPING_DOC_STRING = """
Custom mapping of error codes to error messages if Skyvern encounters an error.
"""

MAX_STEPS_DOC_STRING = """
Maximum number of steps the task can take. Task will fail if it exceeds this number. Cautions: you are charged per step so please set this number to a reasonable value. Contact sales@skyvern.com for custom pricing.
"""

WEBHOOK_URL_DOC_STRING = """
After a run is finished, send an update to this URL. Refer to https://www.skyvern.com/docs/running-tasks/webhooks-faq for more details.
"""

TOTP_IDENTIFIER_DOC_STRING = """
Identifier for the TOTP/2FA/MFA code when the code is pushed to Skyvern. Refer to https://www.skyvern.com/docs/credentials/totp#option-3-push-code-to-skyvern for more details.
"""

TOTP_URL_DOC_STRING = """
URL that serves TOTP/2FA/MFA codes for Skyvern to use during the workflow run. Refer to https://www.skyvern.com/docs/credentials/totp#option-2-get-code-from-your-endpoint for more details.
"""

BROWSER_SESSION_ID_DOC_STRING = """
Run the task or workflow in the specific Skyvern browser session. Having a browser session can persist the real-time state of the browser, so that the next run can continue from where the previous run left off.
"""

MODEL_CONFIG = """
Optional model configuration.
"""
