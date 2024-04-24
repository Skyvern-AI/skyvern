export const urlDescription =
  "The starting URL for the task. This field is required.";

export const webhookCallbackUrlDescription =
  "The URL to call with the results when the task is completed.";

export const navigationGoalDescription =
  "The user's goal for the task. Nullable if the task is only for data extraction.";

export const dataExtractionGoalDescription =
  "The user's goal for data extraction. Nullable if the task is only for navigation.";

export const navigationPayloadDescription =
  "The user's details needed to achieve the task. This is an unstructured field, and information can be passed in in any format you desire. Skyvern will map this information to the questions on the screen in real-time.";

export const extractedInformationSchemaDescription =
  "(Optional) The requested schema of the extracted information for data extraction goal. This is a JSON object with keys as the field names and values as the data types. The data types can be any of the following: string, number, boolean, date, datetime, time, float, integer, object, array, null. If the schema is not provided, Skyvern will infer the schema from the extracted data.";
