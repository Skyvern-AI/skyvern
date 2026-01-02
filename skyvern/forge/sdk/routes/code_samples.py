# Agent
RUN_TASK_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.run_task(prompt="What's the top post on hackernews?")
"""
RUN_TASK_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.runTask({
    body: {
        prompt: "Find the top 3 posts on Hacker News."
    }
})
"""
RUN_WORKFLOW_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.run_workflow(workflow_id="wpid_123", parameters={"parameter1": "value1", "parameter2": "value2"})
"""
RUN_WORKFLOW_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.runWorkflow({
    body: {
        workflow_id: "wpid_123",
        parameters: { parameter1: "value1", parameter2: "value2" }
    }
});
"""
GET_RUN_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
run = await skyvern.get_run(run_id="tsk_v2_123")
print(run)
"""
GET_RUN_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const run = await skyvern.getRun("tsk_v2_123");
console.log(run);
"""
CANCEL_RUN_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.cancel_run(run_id="tsk_v2_123")
"""
CANCEL_RUN_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.cancelRun("tsk_v2_123");
"""
RETRY_RUN_WEBHOOK_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.retry_run_webhook(run_id="tsk_v2_123")
"""
RETRY_RUN_WEBHOOK_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.retryRunWebhook("tsk_v2_123");
"""
GET_RUN_TIMELINE_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
# Get timeline for a workflow run
timeline = await skyvern.get_run_timeline(run_id="wr_123")
print(timeline)

# Get timeline for a task_v2 run
timeline = await skyvern.get_run_timeline(run_id="tsk_v2_123")
print(timeline)
"""
GET_RUN_TIMELINE_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
// Get timeline for a workflow run
const timeline = await skyvern.getRunTimeline("wr_123");
console.log(timeline);

// Get timeline for a task_v2 run
const timeline2 = await skyvern.getRunTimeline("tsk_v2_123");
console.log(timeline2);
"""
LOGIN_CODE_SAMPLE_SKYVERN_PYTHON = """# Login with password saved in Skyvern
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.login(
    url="https://example.com",
    credential_type="skyvern",
    credential_id="cred_123"),
)
"""
LOGIN_CODE_SAMPLE_SKYVERN_TS = """// Login with password saved in Skyvern
import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.login({
    url: "https://example.com",
    credential_type: "skyvern",
    credential_id: "cred_123"
});
"""
LOGIN_CODE_SAMPLE_BITWARDEN_PYTHON = """# Login with password saved in Bitwarden
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
LOGIN_CODE_SAMPLE_BITWARDEN_TS = """// Login with password saved in Bitwarden
import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
// Login with a Bitwarden collection and website url filter
await skyvern.login({
    url: "https://example.com",
    credential_type: "bitwarden",
    bitwarden_collection_id: "BITWARDEN COLLECTION ID"
});

// Login with a Bitwarden item
await skyvern.login({
    url: "https://example.com",
    credential_type: "bitwarden",
    bitwarden_item_id: "BITWARDEN ITEM ID"
});
"""
LOGIN_CODE_SAMPLE_ONEPASSWORD_PYTHON = """# Login with password saved in 1Password
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.login(
    url="https://example.com",
    credential_type="onepassword",
    onepassword_vault_id="1PASSWORD VAULT ID",
    onepassword_item_id="1PASSWORD ITEM ID",
)
"""
LOGIN_CODE_SAMPLE_ONEPASSWORD_TS = """// Login with password saved in 1Password
import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.login({
    url: "https://example.com",
    credential_type: "onepassword",
    onepassword_vault_id: "1PASSWORD VAULT ID",
    onepassword_item_id: "1PASSWORD ITEM ID"
});
"""
DOWNLOAD_FILES_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.download_files(
    url="https://example.com/downloads",
    navigation_goal="Navigate to the downloads page and click the 'Download PDF' button",
    download_suffix="report.pdf"
)
"""
DOWNLOAD_FILES_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.downloadFiles({
    url: "https://example.com/downloads",
    navigation_goal: "Navigate to the downloads page and click the 'Download PDF' button",
    download_suffix: "report.pdf"
});
"""

# Workflows
CREATE_WORKFLOW_CODE_SAMPLE_CURL = """curl -X POST https://api.skyvern.com/v1/workflows \
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
            },
        ],
    },
}
skyvern = Skyvern(api_key="YOUR_API_KEY")
workflow = await skyvern.create_workflow(json_definition=workflow_definition)
print(workflow)
"""
CREATE_WORKFLOW_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });

// Create a workflow in JSON format
const workflowDefinition = {
    title: "Contact Forms Workflow",
    description: "Fill the contact form on the website",
    proxy_location: "RESIDENTIAL",
    webhook_callback_url: "https://example.com/webhook",
    totp_verification_url: "https://example.com/totp",
    totp_identifier: "4155555555",
    model: { name: "gpt-4.1" },
    workflow_definition: {
        parameters: [
            {
                key: "website_url",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            },
            {
                key: "name",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            },
            {
                key: "additional_information",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: "Message: I'd love to learn more about your...\\nPhone: 123-456-7890\\nInquiry type: sales\\nOptional Subject: Hello from [Company Here]"
            },
            {
                key: "email",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            }
        ],
        blocks: [
            {
                label: "Fill_Out_Contact_Form",
                continue_on_failure: true,
                block_type: "navigation",
                url: "{{website_url}}",
                title: "Fill_Out_Contact_Form",
                engine: "skyvern-1.0",
                navigation_goal: "Find the contact form. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. In the case you can't find a contact us form, terminate.\\n\\nFill out required fields as best you can using the following information:\\n{{name}}\\n{{email}}\\n{{additional_information}}",
                error_code_mapping: null,
                max_retries: 0,
                max_steps_per_run: null,
                complete_on_download: false,
                download_suffix: null,
                parameter_keys: [],
                totp_identifier: null,
                totp_verification_url: null,
                complete_criterion: "",
                terminate_criterion: "",
                include_action_history_in_verification: false
            },
            {
                label: "Extract_Email",
                continue_on_failure: false,
                block_type: "extraction",
                url: "",
                title: "Extract_Email",
                data_extraction_goal: "Extract a company email if available ",
                data_schema: null,
                max_retries: 0,
                max_steps_per_run: null,
                parameter_keys: [],
            }
        ]
    }
};

const workflow = await skyvern.createWorkflow({
    json_definition: workflowDefinition
});
console.log(workflow);
"""
UPDATE_WORKFLOW_CODE_SAMPLE_CURL = """curl -X POST https://api.skyvern.com/v1/workflows/wpid_123 \
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
            },
        ],
    },
}
skyvern = Skyvern(api_key="YOUR_API_KEY")
workflow = await skyvern.update_workflow(workflow_id="wpid_123", json_definition=updated_workflow_definition)
print(workflow)
"""
UPDATE_WORKFLOW_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });

const updatedWorkflowDefinition = {
    title: "Updated Contact Forms Workflow",
    description: "Fill the contact form on the website",
    proxy_location: "RESIDENTIAL",
    webhook_callback_url: "https://example.com/webhook",
    totp_verification_url: "https://example.com/totp",
    totp_identifier: "4155555555",
    model: { name: "gpt-4.1" },
    workflow_definition: {
        parameters: [
            {
                key: "website_url",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            },
            {
                key: "name",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            },
            {
                key: "additional_information",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: "Message: I'd love to learn more about your...\\nPhone: 123-456-7890\\nInquiry type: sales\\nOptional Subject: Hello from [Company Here]"
            },
            {
                key: "email",
                description: null,
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: null
            }
        ],
        blocks: [
            {
                label: "Fill_Out_Contact_Form",
                continue_on_failure: true,
                block_type: "navigation",
                url: "{{website_url}}",
                title: "Fill_Out_Contact_Form",
                engine: "skyvern-1.0",
                navigation_goal: "Find the contact form. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. In the case you can't find a contact us form, terminate.\\n\\nFill out required fields as best you can using the following information:\\n{{name}}\\n{{email}}\\n{{additional_information}}",
                error_code_mapping: null,
                max_retries: 0,
                max_steps_per_run: null,
                complete_on_download: false,
                download_suffix: null,
                parameter_keys: [],
                totp_identifier: null,
                totp_verification_url: null,
                complete_criterion: "",
                terminate_criterion: "",
                include_action_history_in_verification: false
            },
            {
                label: "Extract_Email",
                continue_on_failure: false,
                block_type: "extraction",
                url: "",
                title: "Extract_Email",
                data_extraction_goal: "Extract a company email if available ",
                data_schema: null,
                max_retries: 0,
                max_steps_per_run: null,
                parameter_keys: []
            }
        ]
    }
};

const workflow = await skyvern.updateWorkflow("wpid_123", {
    json_definition: updatedWorkflowDefinition
});
console.log(workflow);
"""
DELETE_WORKFLOW_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.delete_workflow(workflow_id="wpid_123")
"""
DELETE_WORKFLOW_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.deleteWorkflow("wpid_123");
"""
GET_WORKFLOWS_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
workflows = await skyvern.get_workflows()
print(workflows)
"""
GET_WORKFLOWS_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const workflows = await skyvern.getWorkflows();
console.log(workflows);
"""

# Credentials
SEND_TOTP_CODE_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.send_totp_code(
    totp_identifier="john.doe@example.com",
    content="Hello, your verification code is 123456"
)
"""
SEND_TOTP_CODE_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.sendTotpCode({
    totp_identifier: "john.doe@example.com",
    content: "Hello, your verification code is 123456"
});
"""
CREATE_CREDENTIAL_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.create_credential(
    name="My Credential",
    credential_type="password",
    credential={"username": "username", "password": "password"},
)
"""
CREATE_CREDENTIAL_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.createCredential({
    name: "My Credential",
    credential_type: "password",
    credential: { username: "username", password: "password" }
});
"""
CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_PYTHON = """from skyvern import Skyvern

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
CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.createCredential({
    name: "My Credit Card",
    credential_type: "credit_card",
    credential: {
        card_number: "4242424242424242",
        card_cvv: "424",
        card_exp_month: "12",
        card_exp_year: "2028",
        card_brand: "visa",
        card_holder_name: "John Doe"
    }
});
"""
DELETE_CREDENTIAL_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.delete_credential(credential_id="cred_123")
"""
DELETE_CREDENTIAL_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.deleteCredential("cred_123");
"""
GET_CREDENTIAL_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
credential = await skyvern.get_credential(credential_id="cred_123")
print(credential)
"""
GET_CREDENTIAL_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const credential = await skyvern.getCredential("cred_123");
console.log(credential);
"""
GET_CREDENTIALS_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
credentials = await skyvern.get_credentials()
print(credentials)
"""
GET_CREDENTIALS_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const credentials = await skyvern.getCredentials();
console.log(credentials);
"""

# Browser Sessions

CREATE_BROWSER_SESSION_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_session = await skyvern.create_browser_session(timeout=60)
print(browser_session)
"""
CREATE_BROWSER_SESSION_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const browserSession = await skyvern.createBrowserSession({
    timeout: 60
});
console.log(browserSession);
"""
CLOSE_BROWSER_SESSION_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.close_browser_session(browser_session_id="pbs_123")
"""
CLOSE_BROWSER_SESSION_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.closeBrowserSession("pbs_123");
"""
GET_BROWSER_SESSION_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_session = await skyvern.get_browser_session(browser_session_id="pbs_123")
print(browser_session)
"""
GET_BROWSER_SESSION_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const browserSession = await skyvern.getBrowserSession("pbs_123");
console.log(browserSession);
"""
GET_BROWSER_SESSIONS_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_sessions = await skyvern.get_browser_sessions()
print(browser_sessions)
"""
GET_BROWSER_SESSIONS_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const browserSessions = await skyvern.getBrowserSessions();
console.log(browserSessions);
"""

# Browser Profiles
CREATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
# Create a browser profile from a persistent browser session
browser_profile = await skyvern.browser_profiles.create_browser_profile(
    name="My Profile",
    browser_session_id="pbs_123",
)
print(browser_profile)

# Or create from a workflow run with persist_browser_session=True
browser_profile = await skyvern.browser_profiles.create_browser_profile(
    name="My Profile",
    workflow_run_id="wr_123",
)
print(browser_profile)
"""
CREATE_BROWSER_PROFILE_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
// Create a browser profile from a persistent browser session
const browserProfile = await skyvern.browserProfiles.createBrowserProfile({
    name: "My Profile",
    browser_session_id: "pbs_123",
});
console.log(browserProfile);

// Or create from a workflow run with persist_browser_session=True
const browserProfile2 = await skyvern.browserProfiles.createBrowserProfile({
    name: "My Profile",
    workflow_run_id: "wr_123",
});
console.log(browserProfile2);
"""
GET_BROWSER_PROFILES_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_profiles = await skyvern.browser_profiles.list_browser_profiles()
print(browser_profiles)
"""
GET_BROWSER_PROFILES_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const browserProfiles = await skyvern.browserProfiles.listBrowserProfiles();
console.log(browserProfiles);
"""
GET_BROWSER_PROFILE_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
browser_profile = await skyvern.browser_profiles.get_browser_profile("bp_123")
print(browser_profile)
"""
GET_BROWSER_PROFILE_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
const browserProfile = await skyvern.browserProfiles.getBrowserProfile("bp_123");
console.log(browserProfile);
"""
DELETE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON = """from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
await skyvern.browser_profiles.delete_browser_profile("bp_123")
"""
DELETE_BROWSER_PROFILE_CODE_SAMPLE_TS = """import { SkyvernClient } from "@skyvern/client";

const skyvern = new SkyvernClient({ apiKey: "YOUR_API_KEY" });
await skyvern.browserProfiles.deleteBrowserProfile("bp_123");
"""
