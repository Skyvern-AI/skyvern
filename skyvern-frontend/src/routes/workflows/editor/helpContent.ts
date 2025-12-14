export const baseHelpTooltipContent = {
  url: "The URL Skyvern is navigating to. Leave this field blank to pick up from where the last block left off.",
  navigationGoal:
    "Give Skyvern an objective. Make sure to include when the block is complete, when it should self-terminate, and any guardrails. Use {{ parameter_name }} to reference a parameter value",
  parameters:
    'Define placeholder values using the "parameters" drop down that you predefine or redefine run-to-run.',
  dataExtractionGoal:
    "Tell Skyvern what data you would like to scrape at the end of your run.",
  dataSchema: "Specify a format for extracted data in JSON.",
  maxRetries:
    "Specify how many times you would like a block to retry upon failure.",
  maxStepsOverride:
    "Specify the maximum number of steps a block can take in total.",
  completeOnDownload:
    "Allow Skyvern to auto-complete the block when it downloads a file.",
  fileSuffix:
    "The complete filename (without extension) for downloaded files. This replaces the entire filename instead of being appended to a random name.",
  errorCodeMapping:
    "Knowing about why a block terminated can be important, specify error messages here.",
  totpVerificationUrl:
    "If you do not have a TOTP Identifier at hand, but do have an internal system for storing TOTP codes, link the endpoint here.",
  totpIdentifier:
    "If you are running multiple workflows at once, you will need to give the block an identifier to know that this TOTP goes with this block.",
  continueOnFailure:
    "Allow the workflow to continue if it encounters a failure.",
  nextLoopOnFailure:
    "When inside a for loop, continue to the next iteration if this block fails.",
  includeActionHistoryInVerification:
    "Include the action history in the completion verification.",
} as const;

export const basePlaceholderContent = {
  url: "(optional) Navigate to this URL: https://...",
  navigationGoal: "Tell Skyvern what to do.",
  dataExtractionGoal: "What data do you need to extract?",
  maxRetries: "Default: 3",
  maxStepsOverride: "Default: 10",
  downloadSuffix: "Enter the complete filename (without extension)",
  totpVerificationUrl: "Provide your 2FA endpoint",
  totpIdentifier: "Add an ID that links your TOTP to the block",
};

export const helpTooltips = {
  task: baseHelpTooltipContent,
  taskv2: {
    ...baseHelpTooltipContent,
    maxSteps:
      "The maximum number of steps this task will take to achieve its goal.",
  },
  navigation: baseHelpTooltipContent,
  extraction: {
    ...baseHelpTooltipContent,
    dataExtractionGoal:
      "Tell Skyvern what data you would like to scrape. Use {{ parameter_name }} to specify parameters to use.",
  },
  action: {
    ...baseHelpTooltipContent,
    navigationGoal:
      "Specify a single step or action you'd like Skyvern to complete. Actions are one-off tasks like filling a field or interacting with a specific element on the page.\n\nCurrently supported actions are click, input text, upload file, and select. Use {{ parameter_name }} to specify parameters to use.",
  },
  fileDownload: {
    ...baseHelpTooltipContent,
    navigationGoal:
      "Give Skyvern an objective that describes how to download the file.",
  },
  validation: baseHelpTooltipContent,
  textPrompt: {
    ...baseHelpTooltipContent,
    prompt:
      "Write a prompt you would like passed into the LLM and specify the output format, if applicable.",
  },
  login: baseHelpTooltipContent,
  loop: {
    ...baseHelpTooltipContent,
    loopValue:
      "Define the values to iterate over. Use a parameter reference or natural language (e.g., 'Extract links of the top 2 posts'). Natural language automatically creates an extraction block that generates a list of string values. Use {{ current_value }} in the loop to get the current iteration value.",
    nextLoopOnFailure:
      "When enabled, if any block inside the loop fails, the loop will immediately jump to the next iteration instead of stopping.",
  },
  sendEmail: {
    ...baseHelpTooltipContent,
    fileAttachments:
      "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
  },
  upload: {
    ...baseHelpTooltipContent,
    path: "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
  },
  fileUpload: {
    ...baseHelpTooltipContent,
    path: "The path of the folder to upload the files to.",
    storage_type:
      "The type of storage to upload the file to. Currently only S3 is supported. Please contact us if you'd like to integrate other storage types.",
    s3_bucket: "The S3 bucket to upload the file to.",
    aws_access_key_id: "The AWS access key ID to use to upload the file to S3.",
    aws_secret_access_key:
      "The AWS secret access key to use to upload the file to S3.",
    region_name: "The AWS region",
    azure_storage_account_name: "The Azure Storage Account Name.",
    azure_storage_account_key: "The Azure Storage Account Key.",
    azure_blob_container_name: "The Azure Blob Container Name.",
  },
  download: {
    ...baseHelpTooltipContent,
    url: "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
  },
  codeBlock: baseHelpTooltipContent,
  fileParser: {
    ...baseHelpTooltipContent,
    fileUrl:
      "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
  },
  wait: {
    ...baseHelpTooltipContent,
    waitInSeconds:
      "Specify a number for how many seconds to wait. Value must be between 0 and 300 seconds.",
  },
  pdfParser: {
    ...baseHelpTooltipContent,
    fileUrl: "The URL from which the file will be downloaded",
    jsonSchema: "Specify a format for the extracted information from the file",
  },
  url: baseHelpTooltipContent,
  httpRequest: {
    ...baseHelpTooltipContent,
    url: "The URL to send the HTTP request to. You can use {{ parameter_name }} to reference parameters.",
    method: "The HTTP method to use for the request.",
    headers: "HTTP headers to include with the request as JSON object.",
    body: "Request body as JSON object. Only used for POST, PUT, PATCH methods.",
    timeout: "Request timeout in seconds.",
    followRedirects: "Whether to automatically follow HTTP redirects.",
    continueOnFailure:
      "Allow the workflow to continue if the HTTP request fails.",
  },
};

export const placeholders = {
  task: basePlaceholderContent,
  taskv2: {
    ...basePlaceholderContent,
    prompt: "Tell Skyvern what to do",
  },
  navigation: {
    ...basePlaceholderContent,
    navigationGoal:
      "Navigate to the product page for product with id {{ product_id }}",
  },
  extraction: {
    ...basePlaceholderContent,
    dataExtractionGoal:
      "Extract the price of the product with id {{ product_id }}",
  },
  action: {
    ...basePlaceholderContent,
    navigationGoal: 'Input {{ name }} into "Name" field.',
  },
  fileDownload: {
    navigationGoal: "Tell Skyvern which file to download.",
  },
  validation: basePlaceholderContent,
  textPrompt: basePlaceholderContent,
  login: {
    ...basePlaceholderContent,
    navigationGoal: "Login to the website using the {{ credentials }}",
  },
  loop: basePlaceholderContent,
  sendEmail: basePlaceholderContent,
  upload: basePlaceholderContent,
  fileUpload: basePlaceholderContent,
  download: basePlaceholderContent,
  codeBlock: basePlaceholderContent,
  fileUrl: basePlaceholderContent,
  wait: basePlaceholderContent,
  pdfParser: basePlaceholderContent,
  url: {
    ...basePlaceholderContent,
    url: "(required) Navigate to this URL: https://...",
  },
  httpRequest: {
    ...basePlaceholderContent,
    url: "https://api.example.com/endpoint",
    headers:
      '{\n  "Content-Type": "application/json",\n  "Authorization": "Bearer {{ token }}"\n}',
    body: '{\n  "key": "value",\n  "parameter": "{{ parameter_name }}"\n}',
  },
  scripts: {
    scriptKey: "my-{{param1}}-{{param2}}-key",
  },
  sequentialKey: "my-{{param1}}-{{param2}}-sequential",
};
