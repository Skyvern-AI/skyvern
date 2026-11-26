import {
  editableInputs,
  fieldLabel,
  findIntegrationAction,
  isTemplate,
  schemaType,
  type IntegrationActionNode,
  type JsonSchema,
  type IntegrationCatalogEntryWithActions,
} from "./types";

// Mirrors the server's _accepts_text: a template renders to text, so only a field that takes text can hold one.
const acceptsText = (schema: JsonSchema | undefined): boolean =>
  schema?.type === "string" || (schema?.anyOf ?? []).some(acceptsText);

const isBlank = (value: unknown) =>
  value === undefined ||
  value === null ||
  (typeof value === "string" && !value.trim());

export function entryNeedsAccount(entry: IntegrationCatalogEntryWithActions) {
  return Boolean(entry.oauthProvider || entry.credentialBackend);
}

// Without a loaded catalog only the ids are checked; the server checks inputs on save.
export function validateIntegrationActionNode(
  node: IntegrationActionNode,
  catalog: readonly IntegrationCatalogEntryWithActions[] | undefined,
): Array<string> {
  const { label, integrationId, actionId, connectionId, inputs } = node.data;
  if (!integrationId.trim()) {
    return [`${label}: Pick an app action.`];
  }
  if (!actionId.trim()) return [`${label}: Action is required.`];
  const found = findIntegrationAction(catalog, integrationId, actionId);
  // An action missing from this org's catalog only warns on the node; the server rejects it at run time.
  if (!found) return [];
  const errors: Array<string> = [];
  if (entryNeedsAccount(found.entry) && !connectionId.trim()) {
    errors.push(`${label}: ${found.entry.name} account is required.`);
  }
  const fields = editableInputs(found.action.inputs);
  const required = new Set(found.action.inputs.required ?? []);
  for (const [key, schema] of fields) {
    // The server checks inputs as saved and fills in no defaults, so a required field needs a value.
    const replySubject =
      integrationId === "gmail" &&
      actionId === "send_email" &&
      key === "subject" &&
      typeof inputs.reply_to_message_id === "string" &&
      Boolean(inputs.reply_to_message_id.trim());
    if (required.has(key) && isBlank(inputs[key]) && !replySubject) {
      errors.push(`${label}: ${fieldLabel(key, schema)} is required.`);
    }
  }
  for (const [key, schema] of fields) {
    const value = inputs[key];
    if (isBlank(value)) continue;
    const type = schemaType(schema);
    const name = fieldLabel(key, schema);
    if (isTemplate(value)) {
      if (!acceptsText(found.action.inputs.properties?.[key])) {
        errors.push(
          `${label}: ${name} is not text, so it cannot use a template.`,
        );
      }
      continue;
    }
    if (schema.enum) {
      if (!schema.enum.includes(value)) {
        errors.push(
          `${label}: ${name} must be one of ${schema.enum.map(String).join(", ")}.`,
        );
      }
    } else if (type === "boolean") {
      if (typeof value !== "boolean") {
        errors.push(`${label}: ${name} must be true or false.`);
      }
    } else if (type === "object" || type === "array") {
      if (typeof value === "string") {
        errors.push(`${label}: ${name} is not valid JSON.`);
      } else if (
        (type === "array") !== Array.isArray(value) ||
        typeof value !== "object"
      ) {
        errors.push(
          `${label}: ${name} must be a JSON ${type === "array" ? "list" : "object"}.`,
        );
      }
    } else if (type === "number" || type === "integer") {
      if (typeof value !== "number") {
        errors.push(`${label}: ${name} must be a number.`);
      } else if (type === "integer" && !Number.isInteger(value)) {
        errors.push(`${label}: ${name} must be a whole number.`);
      }
    }
  }
  return errors;
}
