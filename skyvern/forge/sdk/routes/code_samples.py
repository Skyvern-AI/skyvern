# Agent
RUN_TASK_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.run_task(prompt="What's the top post on hackernews?")
"""
RUN_WORKFLOW_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.run_workflow(workflow_id="wpid_123", parameters={"parameter1": "value1", "parameter2": "value2"})
"""
GET_RUN_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
run = await skyvern.get_run(run_id="tsk_v2_123")
print(run)
"""
CANCEL_RUN_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.cancel_run(run_id="tsk_v2_123")
"""
RETRY_RUN_WEBHOOK_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.retry_run_webhook(run_id="tsk_v2_123")
"""
LOGIN_CODE_SAMPLE_SKYVERN = """# Login with password saved in Skyvern
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.login(
    url="https://example.com",
    credential_type="skyvern",
    credential_id="cred_123"),
)
"""
LOGIN_CODE_SAMPLE_BITWARDEN = """# Login with password saved in Bitwarden
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
# Login with a Bitwarden collection and website url filter
await skyvern.login(
    url="https://example.com",
    credential_type="bitwarden",
    bitwarden_collection_id="BITWARDEN COLLECTION ID",
)

# Login with a Bitwarden item
await skyvern.login(
    url="https://example.com",
    credential_type="bitwarden",
    bitwarden_item_id="BITWARDEN ITEM ID",
)
"""
LOGIN_CODE_SAMPLE_ONEPASSWORD = """# Login with password saved in 1Password
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.login(
    url="https://example.com",
    credential_type="onepassword",
    onepassword_vault_id="1PASSWORD VAULT ID",
    onepassword_item_id="1PASSWORD ITEM ID",
)
"""

# Workflows
CREATE_WORKFLOW_CODE_SAMPLE = """curl -X POST https://api.skyvern.com/v1/workflows \
--header 'x-api-key: {{x-api-key}}' \
--header 'Content-Type: text/plain' \
--data-raw 'title: Contact Forms
description: Fill the contact form on the website
proxy_location: RESIDENTIAL
webhook_callback_url: https://example.com/webhook
totp_verification_url: https://example.com/totp
persist_browser_session: false
model:
  name: gpt-4.1
workflow_definition:
  parameters:
    - key: website_url
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
    - key: name
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
    - key: additional_information
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: |-
        Message: I'd love to learn more about your...
        Phone: 123-456-7890
        Inquiry type: sales
        Optional Subject: Hello from [Company Here]
    - key: email
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
  blocks:
    - label: Fill_Out_Contact_Form
      continue_on_failure: true
      block_type: navigation
      url: "{{website_url}}"
      title: Fill_Out_Contact_Form
      engine: skyvern-1.0
      navigation_goal: >-
        Find the contact form. Fill out the contact us form and submit it. Your
        goal is complete when the page says your message has been sent. In the
        case you can't find a contact us form, terminate.


        Fill out required fields as best you can using the following
        information:

        {{name}}

        {{email}}

        {{additional_information}}
      error_code_mapping: null
      max_retries: 0
      max_steps_per_run: null
      complete_on_download: false
      download_suffix: null
      parameter_keys: []
      totp_identifier: null
      totp_verification_url: null
      cache_actions: false
      complete_criterion: ""
      terminate_criterion: ""
      include_action_history_in_verification: false
    - label: Extract_Email
      continue_on_failure: false
      block_type: extraction
      url: ""
      title: Extract_Email
      data_extraction_goal: "Extract a company email if available "
      data_schema: null
      max_retries: 0
      max_steps_per_run: null
      parameter_keys: []
      cache_actions: false
'
"""
CREATE_WORKFLOW_CODE_SAMPLE_PYTHON = """
from skyvern import Skyvern

# Create a workflow in JSON format
workflow_definition = {
    "title": "Contact Forms Workflow",
    "description": "Fill the contact form on the website",
    "proxy_location": "RESIDENTIAL",
    "webhook_callback_url": "https://example.com/webhook",
    "totp_verification_url": "https://example.com/totp",
    "totp_identifier": "4155555555",
    "model": {"name": "gpt-4.1"},
    "workflow_definition": {
        "parameters": [
            {
                "key": "website_url",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
            {
                "key": "name",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
            {
                "key": "additional_information",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": "Message: I'd love to learn more about your...\nPhone: 123-456-7890\nInquiry type: sales\nOptional Subject: Hello from [Company Here]",
            },
            {
                "key": "email",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
        ],
        "blocks": [
            {
                "label": "Fill_Out_Contact_Form",
                "continue_on_failure": True,
                "block_type": "navigation",
                "url": "{{website_url}}",
                "title": "Fill_Out_Contact_Form",
                "engine": "skyvern-1.0",
                "navigation_goal": "Find the contact form. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. In the case you can't find a contact us form, terminate.\n\nFill out required fields as best you can using the following information:\n{{name}}\n{{email}}\n{{additional_information}}",
                "error_code_mapping": None,
                "max_retries": 0,
                "max_steps_per_run": None,
                "complete_on_download": False,
                "download_suffix": None,
                "parameter_keys": [],
                "totp_identifier": None,
                "totp_verification_url": None,
                "cache_actions": False,
                "complete_criterion": "",
                "terminate_criterion": "",
                "include_action_history_in_verification": False,
            },
            {
                "label": "Extract_Email",
                "continue_on_failure": False,
                "block_type": "extraction",
                "url": "",
                "title": "Extract_Email",
                "data_extraction_goal": "Extract a company email if available ",
                "data_schema": None,
                "max_retries": 0,
                "max_steps_per_run": None,
                "parameter_keys": [],
                "cache_actions": False,
            },
        ],
    },
}
skyvern = Skyvern(api_key="YOUR_API_KEY")
workflow = await skyvern.create_workflow(json_definition=workflow_definition)
print(workflow)
"""
UPDATE_WORKFLOW_CODE_SAMPLE = """curl -X POST https://api.skyvern.com/v1/workflows/wpid_123 \
--header 'x-api-key: {{x-api-key}}' \
--header 'Content-Type: text/plain' \
--data-raw 'title: Contact Forms
description: Fill the contact form on the website
proxy_location: RESIDENTIAL
webhook_callback_url: https://example.com/webhook
totp_verification_url: https://example.com/totp
persist_browser_session: false
model:
  name: gpt-4.1
workflow_definition:
  parameters:
    - key: website_url
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
    - key: name
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
    - key: additional_information
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: |-
        Message: I'd love to learn more about your...
        Phone: 123-456-7890
        Inquiry type: sales
        Optional Subject: Hello from [Company Here]
    - key: email
      description: null
      parameter_type: workflow
      workflow_parameter_type: string
      default_value: null
  blocks:
    - label: Fill_Out_Contact_Form
      continue_on_failure: true
      block_type: navigation
      url: "{{website_url}}"
      title: Fill_Out_Contact_Form
      engine: skyvern-1.0
      navigation_goal: >-
        Find the contact form. Fill out the contact us form and submit it. Your
        goal is complete when the page says your message has been sent. In the
        case you can't find a contact us form, terminate.


        Fill out required fields as best you can using the following
        information:

        {{name}}

        {{email}}

        {{additional_information}}
      error_code_mapping: null
      max_retries: 0
      max_steps_per_run: null
      complete_on_download: false
      download_suffix: null
      parameter_keys: []
      totp_identifier: null
      totp_verification_url: null
      cache_actions: false
      complete_criterion: ""
      terminate_criterion: ""
      include_action_history_in_verification: false
    - label: Extract_Email
      continue_on_failure: false
      block_type: extraction
      url: ""
      title: Extract_Email
      data_extraction_goal: "Extract a company email if available "
      data_schema: null
      max_retries: 0
      max_steps_per_run: null
      parameter_keys: []
      cache_actions: false
'
"""
UPDATE_WORKFLOW_CODE_SAMPLE_PYTHON = """
from skyvern import Skyvern

updated_workflow_definition = {
    "title": "Updated Contact Forms Workflow",
    "description": "Fill the contact form on the website",
    "proxy_location": "RESIDENTIAL",
    "webhook_callback_url": "https://example.com/webhook",
    "totp_verification_url": "https://example.com/totp",
    "totp_identifier": "4155555555",
    "model": {"name": "gpt-4.1"},
    "workflow_definition": {
        "parameters": [
            {
                "key": "website_url",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
            {
                "key": "name",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
            {
                "key": "additional_information",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": "Message: I'd love to learn more about your...\nPhone: 123-456-7890\nInquiry type: sales\nOptional Subject: Hello from [Company Here]",
            },
            {
                "key": "email",
                "description": None,
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "default_value": None,
            },
        ],
        "blocks": [
            {
                "label": "Fill_Out_Contact_Form",
                "continue_on_failure": True,
                "block_type": "navigation",
                "url": "{{website_url}}",
                "title": "Fill_Out_Contact_Form",
                "engine": "skyvern-1.0",
                "navigation_goal": "Find the contact form. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. In the case you can't find a contact us form, terminate.\n\nFill out required fields as best you can using the following information:\n{{name}}\n{{email}}\n{{additional_information}}",
                "error_code_mapping": None,
                "max_retries": 0,
                "max_steps_per_run": None,
                "complete_on_download": False,
                "download_suffix": None,
                "parameter_keys": [],
                "totp_identifier": None,
                "totp_verification_url": None,
                "cache_actions": False,
                "complete_criterion": "",
                "terminate_criterion": "",
                "include_action_history_in_verification": False,
            },
            {
                "label": "Extract_Email",
                "continue_on_failure": False,
                "block_type": "extraction",
                "url": "",
                "title": "Extract_Email",
                "data_extraction_goal": "Extract a company email if available ",
                "data_schema": None,
                "max_retries": 0,
                "max_steps_per_run": None,
                "parameter_keys": [],
                "cache_actions": False,
            },
        ],
    },
}
skyvern = Skyvern(api_key="YOUR_API_KEY")
workflow = await skyvern.update_workflow(workflow_id="wpid_123", json_definition=updated_workflow_definition)
print(workflow)
"""
DELETE_WORKFLOW_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.delete_workflow(workflow_id="wpid_123")
"""
GET_WORKFLOWS_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
workflows = await skyvern.get_workflows()
print(workflows)
"""

# Credentials
SEND_TOTP_CODE_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.send_totp_code(totp_code="123456")
"""
CREATE_CREDENTIAL_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.create_credential(
    name="My Credential",
    credential_type="password",
    credential={"username": "username", "password": "password"},
)
"""
CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.create_credential(
    name="My Credit Card",
    credential_type="credit_card",
    credential={
        "card_number": "4242424242424242",
        "card_cvv": "424",
        "card_exp_month": "12",
        "card_exp_year": "2028",
        "card_brand": "visa",
        "card_holder_name": "John Doe",
    },
)
"""
DELETE_CREDENTIAL_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.delete_credential(credential_id="cred_123")
"""
GET_CREDENTIAL_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
credential = await skyvern.get_credential(credential_id="cred_123")
print(credential)
"""
GET_CREDENTIALS_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
credentials = await skyvern.get_credentials()
print(credentials)
"""

# Browser Sessions

CREATE_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_session = await skyvern.create_browser_session(timeout=60)
print(browser_session)
"""
CLOSE_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.close_browser_session(browser_session_id="pbs_123")
"""
GET_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_session = await skyvern.get_browser_session(browser_session_id="pbs_123")
print(browser_session)
"""
GET_BROWSER_SESSIONS_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_sessions = await skyvern.get_browser_sessions()
print(browser_sessions)
"""
