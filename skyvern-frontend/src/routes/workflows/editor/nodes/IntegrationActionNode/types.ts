import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import type { IntegrationCatalogEntry } from "@/hooks/useIntegrationCatalogQuery";

export type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: unknown[];
  format?: string;
  default?: unknown;
  const?: unknown;
  anyOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  required?: string[];
};

export type IntegrationActionSpec = {
  id: string;
  name: string;
  description: string;
  inputs: JsonSchema;
  needs?: string[];
  acts?: boolean;
  credentialBackends?: CredentialBackend[];
  runsVia?: string;
};

export function isGenericActionAvailable(
  action: IntegrationActionSpec,
): boolean {
  return action.inputs.properties?.credential_source?.const !== "nango";
}

const BUILT_IN_BLOCKS = {
  send_email: "sendEmail",
  email_inbox: "emailInbox",
  file_download: "fileDownload",
  file_upload: "fileUpload",
  google_sheets_read: "googleSheetsRead",
  google_sheets_write: "googleSheetsWrite",
} as const;

// An action that one of our blocks runs is added as that block, preset with the
// action's pinned inputs, so the library's app rows and block rows open one editor.
export function builtInBlockFor(action: IntegrationActionSpec) {
  const blockType = action.runsVia?.startsWith("block:")
    ? action.runsVia.slice("block:".length)
    : "";
  if (!Object.prototype.hasOwnProperty.call(BUILT_IN_BLOCKS, blockType)) {
    return null;
  }
  const key = blockType as keyof typeof BUILT_IN_BLOCKS;
  const pinned = Object.entries(action.inputs.properties ?? {}).filter(
    ([, schema]) => schema.const !== undefined,
  );
  return {
    blockType: key,
    nodeType: BUILT_IN_BLOCKS[key],
    data: Object.fromEntries(
      pinned.map(([field, schema]) => [
        field.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
        schema.const,
      ]),
    ),
  };
}

export type IntegrationCatalogEntryWithActions = IntegrationCatalogEntry & {
  name: string;
  logo?: string;
  description?: string;
  oauthProvider?: "google" | "microsoft";
  requiredScopes?: string[];
  capabilityScopes?: string[];
  credentialBackend?: string;
  additionalCredentialBackends?: string[];
  actions?: IntegrationActionSpec[];
  triggers?: Array<{ id: string; name: string }>;
};

export type IntegrationActionNodeData = NodeBaseData & {
  integrationId: string;
  actionId: string;
  connectionId: string;
  inputs: Record<string, unknown>;
  parameterKeys: Array<string>;
};

export type IntegrationActionNode = Node<
  IntegrationActionNodeData,
  "integrationAction"
>;

export const integrationActionNodeDefaultData: IntegrationActionNodeData = {
  debuggable: true,
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  integrationId: "",
  actionId: "",
  connectionId: "",
  inputs: {},
  parameterKeys: [],
};

export function isIntegrationActionNode(
  node: Node,
): node is IntegrationActionNode {
  return node.type === "integrationAction";
}

export function findIntegrationAction(
  catalog: readonly IntegrationCatalogEntryWithActions[] | undefined,
  integrationId: string,
  actionId: string,
) {
  const entry = catalog?.find((item) => item.id === integrationId);
  const action = entry?.actions?.find((item) => item.id === actionId);
  return entry && action ? { entry, action } : null;
}

export function integrationActionTitle(
  entry: { name: string },
  action: { name: string },
) {
  return `${entry.name}: ${action.name}`;
}

export function schemaType(schema: JsonSchema): string | undefined {
  return Array.isArray(schema.type)
    ? schema.type.find((type) => type !== "null")
    : schema.type;
}

// Pydantic writes optional fields as anyOf [X, null] and named enums as $ref; flatten both to X.
export function fieldSchema(
  schema: JsonSchema,
  defs: Record<string, JsonSchema> = {},
): JsonSchema {
  const ref = schema.$ref?.replace("#/$defs/", "");
  const base =
    ref && defs[ref]
      ? { ...defs[ref], ...schema, title: schema.title }
      : schema;
  const branch = base.anyOf?.find((option) => schemaType(option) !== "null");
  if (!branch) return base;
  const inner = fieldSchema(branch, defs);
  return {
    ...inner,
    // A named type's own title ("EmailBodyFormat") is not the field's name, so only the field's title carries over.
    title: base.title,
    description: base.description ?? inner.description,
    default: base.default ?? inner.default,
    format: base.format ?? inner.format,
  };
}

export function fieldLabel(key: string, schema: JsonSchema): string {
  const words = key.replace(/_/g, " ");
  return schema.title ?? words.charAt(0).toUpperCase() + words.slice(1);
}

// Fields pinned with const are filled by the action itself, so the user never sees them.
export function editableInputs(
  inputs: JsonSchema,
): Array<[string, JsonSchema]> {
  return Object.entries(inputs.properties ?? {})
    .filter(([, schema]) => schema.const === undefined)
    .map(([key, schema]) => [key, fieldSchema(schema, inputs.$defs)]);
}

// Text that is a template is resolved at run time, so it is never checked as JSON or a number here.
export function isTemplate(value: unknown): boolean {
  return typeof value === "string" && /\{\{|\{%/.test(value);
}

export type CredentialBackend = "native" | "nango";

// An account's backend comes from looking it up among loaded accounts, never from its id (ADR 0033 rule 3).
// An unknown account, or an action that declares no credentialBackends, is left to the server.
export function actionAcceptsAccount(
  action: IntegrationActionSpec | undefined,
  backend: CredentialBackend | undefined,
): boolean {
  const backends = action?.credentialBackends;
  return !backends || backend === undefined || backends.includes(backend);
}

export type AccountFit = "yes" | "no" | "unknown";
