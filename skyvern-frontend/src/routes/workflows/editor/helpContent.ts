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
    "If this block fails, skip the failure and continue to the next block in the current iteration. The remaining blocks in the same iteration will still run.",
  nextLoopOnFailure:
    "If this block fails, skip the remaining blocks in the current iteration and jump to the next loop iteration.",
  onBlockFailure:
    "Choose what happens when this block fails. 'Continue to next block in this iteration' swallows the failure and runs the rest of the iteration. 'Skip to next iteration' abandons the iteration and starts the next one.",
  includeActionHistoryInVerification:
    "Include the action history in the completion verification.",
  engine:
    "Skyvern 1.0: Fast, single-goal tasks. Skyvern 2.0: Complex, multi-goal tasks (slower).",
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
  google_sheets_read: {
    ...baseHelpTooltipContent,
    spreadsheetUrl:
      "Paste a Google Sheets URL, or click the table icon to pick from your connected account. Jinja templates work too.",
    sheetName:
      "Pick a tab via the layers icon once a spreadsheet is selected, or type the name directly.",
    range: "A1 notation range to read (optional, defaults to all data)",
    credentialId: "The credential ID for Google OAuth authentication",
    hasHeaderRow:
      "If enabled, the first row is used as column headers for the output objects",
  },
  google_sheets_write: {
    ...baseHelpTooltipContent,
    spreadsheetUrl:
      "The full URL of the Google Sheet to write to. Use the picker to browse your connected account.",
    credentialId:
      "The Google account used to authenticate with the spreadsheet.",
    sheetName:
      "The sheet tab to write to. Use the picker to list tabs or create a new one.",
    writeMode:
      "Append adds new rows below existing data. Update Range overwrites the exact cells in the range you specify.",
    range:
      "Only used for Update Range. A1 notation (e.g. A2:D5) or a named range. The data shape must match the range dimensions.",
    values:
      "Jinja2 template that resolves to a JSON array. Arrays of lists write left-to-right; arrays of objects require column mappings below.",
    columnMapping:
      "Map each source field to a sheet column. Use the column letter (A, B, C) or the header name if your sheet has a header row.",
  },
  login: baseHelpTooltipContent,
  loop: {
    ...baseHelpTooltipContent,
    loopValue:
      "Define the values to iterate over. Use a parameter reference or natural language (e.g., 'Extract links of the top 2 posts'). Natural language automatically creates an extraction block that generates a list of string values. Use {{ current_value }} in the loop to get the current iteration value.",
    nextLoopOnFailure:
      "When an iteration fails, skip its remaining blocks and start the next iteration instead of stopping the entire loop.",
    continueOnFailure:
      "If this loop ends in failure, let the rest of the workflow continue running instead of stopping. Does not affect iteration behavior — use 'Skip Iterations that Fail' for that.",
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
    fileType:
      "The format of the file to parse. Auto-detected from the URL extension when possible.",
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
