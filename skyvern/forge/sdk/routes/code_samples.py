RUN_TASK_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.agent.run_task(prompt="What's the top post on hackernews?")
"""
RUN_WORKFLOW_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.agent.run_workflow(workflow_id="wpid_123", parameters={"parameter1": "value1", "parameter2": "value2"})
"""
GET_RUN_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
run = await skyvern.agent.get_run(run_id="tsk_v2_123")
print(run)
"""
CANCEL_RUN_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.cancel_run(run_id="tsk_v2_123")
"""
CREATE_WORKFLOW_CODE_SAMPLE = """curl -X POST https://api.skyvern.com/v1/workflows \
--header 'x-api-key: {{x-api-key}}' \
--header 'Content-Type: application/x-yaml' \
--data-raw 'title: Invoice Downloading Demo (Jun 13)
description: >-
  Login to the website, download all the invoices after a date, email the
  invoices
workflow_definition:
  parameters:
    - key: website_url
      parameter_type: workflow
      workflow_parameter_type: string
    - key: credentials
      parameter_type: bitwarden_login_credential
      bitwarden_client_id_aws_secret_key: SECRET
      bitwarden_client_secret_aws_secret_key: SECRET
      bitwarden_master_password_aws_secret_key: SECRET
      bitwarden_collection_id: SECRET
      url_parameter_key: website_url
    - key: invoice_retrieval_start_date
      parameter_type: workflow
      workflow_parameter_type: string
    - key: smtp_host
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_HOST_AWS_SES
    - key: smtp_port
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_PORT_AWS_SES
    - key: smtp_username
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_USERNAME_SES
    - key: smtp_password
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_PASSWORD_SES
    - parameter_type: context
      key: order_history_url
      source_parameter_key: get_order_history_page_url_and_qualifying_order_ids_output
    - parameter_type: context
      key: order_ids
      source_parameter_key: get_order_history_page_url_and_qualifying_order_ids_output
    - parameter_type: context
      key: order_id
      source_parameter_key: order_ids
  blocks:
    - block_type: task
      label: login
      parameter_keys:
        - credentials
      url: website_url
      navigation_goal: >-
        If you're not on the login page, navigate to login page and login using the credentials given, and then navigate to the personal account page. First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page. Then, try to login and navigate to the personal account page. If you fail to login to find the login page or can't login after several trials, terminate. If you're on the personal account page, consider the goal is completed.
      error_code_mapping:
        stuck_with_popups: terminate and return this error if you can't close popups after several tries and can't take the necessary actions on the website because there is a blocking popup on the page
        failed_to_login: terminate and return this error if you fail logging in to the page
    - block_type: task
      label: get_order_history_page_url_and_qualifying_order_ids
      parameter_keys:
        - invoice_retrieval_start_date
      navigation_goal: Find the order history page. If there is no orders after given start date, terminate.
      data_extraction_goal: >-
        You need to extract the order history page url by looking at the current
        page you're on. You need to extract contact emails you see on the page. You also need to extract the order ids for orders that
        happened on or after invoice_retrieval_start_date. Make sure to filter
        only the orders that happened on or after invoice_retrieval_start_date. You need to compare each order's date with the invoice_download_start_date. You can only include an order in the output if the order's date is after or the same as the invoice_download_start_date.
        While comparing dates, first compare year, then month, then day. invoice_retrieval_start_date
        is in YYYY-MM-DD format. The dates on the websites may be in different formats, compare accordingly and compare year, date, and month.
      error_code_mapping:
        failed_to_find_order_history_page: return this error if you can't find the order history page on the website
        no_orders_found_after_start_date: return this error if there are no orders after the specified invoice_download_start_date
      data_schema:
        type: object
        properties:
          order_history_url:
            type: url
            description: >-
              The exact URL of the order history page. Do not make any
              assumptions. Return the URL that's passed along in this context.
          contact_emails:
            type: array
            items:
                type: string
                description: Contact email for the ecommerce website you're on. If you can't find any return null
          date_comparison_scratchpad:
            type: string
            description: >-
                You are supposed to filter the orders that happened on or after the invoice_download_start_date. Think through how you will approach this task step-by-step here. Consider these before starting the comparison:
                - What format is the order date in? How can you parse it into a structured format?
                - What is the correct way to compare two dates?
                - How will you compare the order dates to the invoice_download_start_date? 
                
                Write out your thought process before filling out the order_ids field below. Remember, the original date may be in any format, so parse it carefully! The invoice_download_start_date will be an exact date you can directly compare against in the format YYYY-MM-DD.
          order_ids:
            type: array
            items:
              type: object
              properties:
                order_date:
                  type: iso-8601-date-string
                order_id:
                  type: string
            description: >-
              Return a list of order id strings. Do not return order ids of
              orders that happened before the specified
              invoice_retrieval_start_date
    - block_type: for_loop
      label: iterate_over_order_ids
      loop_over_parameter_key: order_ids
      continue_on_failure: true
      loop_blocks:
        - block_type: task
          label: download_invoice_for_order
          complete_on_download: true
          continue_on_failure: true
          parameter_keys:
            - order_id
          url: order_history_url
          navigation_goal: Download the invoice of the order with the given order ID. Make sure to download the invoice for the given order id. If the element tree doesn't have a matching order id, check the screenshots. Complete if you have successfully downloaded the invoice according to action history, if you were able to download it, you'll see download_triggered=True for the last step. If you don't see a way to download an invoice, navigate to the order page if possible. If there's no way to download an invoice terminate. If the text suggests printing, you can assume you can download it. Return click action with download=True if you want to trigger a download.
          error_code_mapping:
            not_possible_to_download_invoice: return this error if the website doesn't allow downloading/viewing invoices
            cant_solve_captcha: return this error if captcha isn't solved after multiple retries
    - block_type: upload_to_s3
      label: upload_downloaded_files_to_s3
      path: SKYVERN_DOWNLOAD_DIRECTORY
    - block_type: send_email
      label: send_email
      smtp_host_secret_parameter_key: smtp_host
      smtp_port_secret_parameter_key: smtp_port
      smtp_username_secret_parameter_key: smtp_username
      smtp_password_secret_parameter_key: smtp_password
      sender: hello@skyvern.com
      recipients:
        - founders@skyvern.com
      subject: Skyvern - Downloaded Invoices Demo
      body: website_url
      file_attachments:
        - SKYVERN_DOWNLOAD_DIRECTORY
'
"""
UPDATE_WORKFLOW_CODE_SAMPLE = """curl -X POST https://api.skyvern.com/v1/workflows/wpid_123 \
--header 'x-api-key: {{x-api-key}}' \
--header 'Content-Type: application/x-yaml' \
--data-raw 'title: Invoice Downloading Demo (Jun 13)
description: >-
  Login to the website, download all the invoices after a date, email the
  invoices
workflow_definition:
  parameters:
    - key: website_url
      parameter_type: workflow
      workflow_parameter_type: string
    - key: credentials
      parameter_type: bitwarden_login_credential
      bitwarden_client_id_aws_secret_key: SECRET
      bitwarden_client_secret_aws_secret_key: SECRET
      bitwarden_master_password_aws_secret_key: SECRET
      bitwarden_collection_id: SECRET
      url_parameter_key: website_url
    - key: invoice_retrieval_start_date
      parameter_type: workflow
      workflow_parameter_type: string
    - key: smtp_host
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_HOST_AWS_SES
    - key: smtp_port
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_PORT_AWS_SES
    - key: smtp_username
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_USERNAME_SES
    - key: smtp_password
      parameter_type: aws_secret
      aws_key: SKYVERN_SMTP_PASSWORD_SES
    - parameter_type: context
      key: order_history_url
      source_parameter_key: get_order_history_page_url_and_qualifying_order_ids_output
    - parameter_type: context
      key: order_ids
      source_parameter_key: get_order_history_page_url_and_qualifying_order_ids_output
    - parameter_type: context
      key: order_id
      source_parameter_key: order_ids
  blocks:
    - block_type: task
      label: login
      parameter_keys:
        - credentials
      url: website_url
      navigation_goal: >-
        If you're not on the login page, navigate to login page and login using the credentials given, and then navigate to the personal account page. First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page. Then, try to login and navigate to the personal account page. If you fail to login to find the login page or can't login after several trials, terminate. If you're on the personal account page, consider the goal is completed.
      error_code_mapping:
        stuck_with_popups: terminate and return this error if you can't close popups after several tries and can't take the necessary actions on the website because there is a blocking popup on the page
        failed_to_login: terminate and return this error if you fail logging in to the page
    - block_type: task
      label: get_order_history_page_url_and_qualifying_order_ids
      parameter_keys:
        - invoice_retrieval_start_date
      navigation_goal: Find the order history page. If there is no orders after given start date, terminate.
      data_extraction_goal: >-
        You need to extract the order history page url by looking at the current
        page you're on. You need to extract contact emails you see on the page. You also need to extract the order ids for orders that
        happened on or after invoice_retrieval_start_date. Make sure to filter
        only the orders that happened on or after invoice_retrieval_start_date. You need to compare each order's date with the invoice_download_start_date. You can only include an order in the output if the order's date is after or the same as the invoice_download_start_date.
        While comparing dates, first compare year, then month, then day. invoice_retrieval_start_date
        is in YYYY-MM-DD format. The dates on the websites may be in different formats, compare accordingly and compare year, date, and month.
      error_code_mapping:
        failed_to_find_order_history_page: return this error if you can't find the order history page on the website
        no_orders_found_after_start_date: return this error if there are no orders after the specified invoice_download_start_date
      data_schema:
        type: object
        properties:
          order_history_url:
            type: url
            description: >-
              The exact URL of the order history page. Do not make any
              assumptions. Return the URL that's passed along in this context.
          contact_emails:
            type: array
            items:
                type: string
                description: Contact email for the ecommerce website you're on. If you can't find any return null
          date_comparison_scratchpad:
            type: string
            description: >-
                You are supposed to filter the orders that happened on or after the invoice_download_start_date. Think through how you will approach this task step-by-step here. Consider these before starting the comparison:
                - What format is the order date in? How can you parse it into a structured format?
                - What is the correct way to compare two dates?
                - How will you compare the order dates to the invoice_download_start_date? 
                
                Write out your thought process before filling out the order_ids field below. Remember, the original date may be in any format, so parse it carefully! The invoice_download_start_date will be an exact date you can directly compare against in the format YYYY-MM-DD.
          order_ids:
            type: array
            items:
              type: object
              properties:
                order_date:
                  type: iso-8601-date-string
                order_id:
                  type: string
            description: >-
              Return a list of order id strings. Do not return order ids of
              orders that happened before the specified
              invoice_retrieval_start_date
    - block_type: for_loop
      label: iterate_over_order_ids
      loop_over_parameter_key: order_ids
      continue_on_failure: true
      loop_blocks:
        - block_type: task
          label: download_invoice_for_order
          complete_on_download: true
          continue_on_failure: true
          parameter_keys:
            - order_id
          url: order_history_url
          navigation_goal: Download the invoice of the order with the given order ID. Make sure to download the invoice for the given order id. If the element tree doesn't have a matching order id, check the screenshots. Complete if you have successfully downloaded the invoice according to action history, if you were able to download it, you'll see download_triggered=True for the last step. If you don't see a way to download an invoice, navigate to the order page if possible. If there's no way to download an invoice terminate. If the text suggests printing, you can assume you can download it. Return click action with download=True if you want to trigger a download.
          error_code_mapping:
            not_possible_to_download_invoice: return this error if the website doesn't allow downloading/viewing invoices
            cant_solve_captcha: return this error if captcha isn't solved after multiple retries
    - block_type: upload_to_s3
      label: upload_downloaded_files_to_s3
      path: SKYVERN_DOWNLOAD_DIRECTORY
    - block_type: send_email
      label: send_email
      smtp_host_secret_parameter_key: smtp_host
      smtp_port_secret_parameter_key: smtp_port
      smtp_username_secret_parameter_key: smtp_username
      smtp_password_secret_parameter_key: smtp_password
      sender: hello@skyvern.com
      recipients:
        - founders@skyvern.com
      subject: Skyvern - Downloaded Invoices Demo
      body: website_url
      file_attachments:
        - SKYVERN_DOWNLOAD_DIRECTORY
'
"""
DELETE_WORKFLOW_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.agent.delete_workflow(workflow_id="wpid_123")
"""
SEND_TOTP_CODE_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.credentials.send_totp_code(totp_code="123456")
"""
CREATE_CREDENTIAL_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.credentials.create_credential(
    name="Amazon Login",
    credential_type="password",
    credential={"username": "user@example.com", "password": "myamazonpassword"},
)
"""
CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.credentials.create_credential(
    name="Amazon Login",
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

skyvern = Skyvern(api_key="your_api_key")
await skyvern.credentials.delete_credential(credential_id="cred_123")
"""
GET_CREDENTIAL_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
credential = await skyvern.credentials.get_credential(credential_id="cred_123")
print(credential)
"""
GET_CREDENTIALS_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
credentials = await skyvern.credentials.get_credentials()
print(credentials)
"""
CREATE_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
browser_session = await skyvern.browser_sessions.create_browser_session(timeout=60)
print(browser_session)
"""
CLOSE_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
await skyvern.browser_sessions.close_browser_session(browser_session_id="pbs_123")
"""
GET_BROWSER_SESSION_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
browser_session = await skyvern.browser_sessions.get_browser_session(browser_session_id="pbs_123")
print(browser_session)
"""
GET_BROWSER_SESSIONS_CODE_SAMPLE = """from skyvern import Skyvern

skyvern = Skyvern(api_key="your_api_key")
browser_sessions = await skyvern.browser_sessions.get_browser_sessions()
print(browser_sessions)
"""
