import { afterEach, describe, expect, test } from "vitest";

import { appRows, matchingAppActions } from "../../panels/appLibrary";
import { queryClient } from "@/api/QueryClient";
import { createNode, getWorkflowErrors } from "../../workflowEditorUtils";
import {
  fieldLabel,
  fieldSchema,
  type IntegrationActionNode,
  type IntegrationCatalogEntryWithActions,
} from "./types";
import { validateIntegrationActionNode } from "./validate";

const sendEmail = (needs: string[]) => ({
  id: "send_email",
  name: "Send email",
  description: "Send an email",
  needs,
  inputs: {
    type: "object",
    properties: {
      to: { type: "string", title: "To" },
      priority: { type: "integer" },
      headers: {
        anyOf: [{ type: "object" }, { type: "null" }],
        default: null,
      },
      body_format: { type: "string", enum: ["text", "html"], default: "text" },
      email_client: { const: "gmail", default: "gmail" },
    },
    required: ["to", "email_client"],
  },
});

const catalog: IntegrationCatalogEntryWithActions[] = [
  {
    id: "outlook",
    name: "Outlook",
    status: "live",
    oauthProvider: "microsoft",
    actions: [sendEmail(["Mail.Send"])],
  },
  {
    id: "gmail",
    name: "Gmail",
    status: "live",
    oauthProvider: "google",
    actions: [
      sendEmail(["gmail.send"]),
      { id: "read_inbox", name: "Read inbox", description: "", inputs: {} },
    ],
    triggers: [{ id: "new_email", name: "New email" }],
  },
  {
    id: "powerbi",
    name: "Power BI",
    status: "live",
    credentialBackend: "nango",
    actions: [sendEmail([])],
  },
  {
    id: "notion",
    name: "Notion",
    status: "comingSoon",
    actions: [sendEmail([])],
  },
];

function actionNode(
  data: Partial<IntegrationActionNode["data"]>,
): IntegrationActionNode {
  const node = createNode({ id: "n1" }, "integrationAction", "send");
  if (node.type !== "integrationAction") throw new Error("wrong node type");
  return { ...node, data: { ...node.data, ...data } };
}

describe("integration action library and validation", () => {
  afterEach(() => queryClient.clear());

  test("lists live apps connected first and finds an action in every app", () => {
    const rows = appRows(catalog, (entry) => entry.id === "gmail");
    expect(rows.map((row) => row.entry.id)).toEqual(["gmail", "outlook"]);
    expect(
      matchingAppActions(rows, "send email").map(
        ({ entry, action }) => `${entry.name}: ${action.name}`,
      ),
    ).toEqual(["Gmail: Send email", "Outlook: Send email"]);
    expect(matchingAppActions(rows, "gmail inbox")).toHaveLength(1);
  });

  test("flags a missing account, missing required inputs and bad typed text", () => {
    const node = actionNode({
      integrationId: "gmail",
      actionId: "send_email",
      inputs: { priority: "soon", headers: "{not json" },
    });
    expect(validateIntegrationActionNode(node, catalog)).toEqual([
      "send: Gmail account is required.",
      "send: To is required.",
      "send: Priority must be a number.",
      "send: Headers is not valid JSON.",
    ]);

    const ok = actionNode({
      integrationId: "gmail",
      actionId: "send_email",
      connectionId: "goc_1",
      inputs: { to: "{{ trigger.email.from }}" },
    });
    expect(validateIntegrationActionNode(ok, catalog)).toEqual([]);
    expect(validateIntegrationActionNode(ok, undefined)).toEqual([]);
    expect(validateIntegrationActionNode(actionNode({}), undefined)).toEqual([
      "send: Pick an app action.",
    ]);
    const list = actionNode({
      ...ok.data,
      inputs: { ...ok.data.inputs, headers: [], priority: 1.5 },
    });
    const templated = actionNode({
      ...ok.data,
      inputs: { ...ok.data.inputs, priority: "{{ n }}" },
    });
    expect(validateIntegrationActionNode(templated, catalog)).toEqual([
      "send: Priority is not text, so it cannot use a template.",
    ]);
    expect(validateIntegrationActionNode(list, catalog)).toEqual([
      "send: Priority must be a whole number.",
      "send: Headers must be a JSON object.",
    ]);
  });

  test("flattens pydantic optional and $ref fields to their real type", () => {
    const schema = fieldSchema(
      {
        anyOf: [{ $ref: "#/$defs/Mode" }, { type: "null" }],
        title: "Mode",
        default: null,
      },
      { Mode: { type: "string", enum: ["append", "update"] } },
    );
    expect(schema).toMatchObject({
      type: "string",
      enum: ["append", "update"],
      title: "Mode",
    });
    const bare = fieldSchema(
      { $ref: "#/$defs/BodyFormat", default: "text" },
      { BodyFormat: { title: "BodyFormat", type: "string", enum: ["text"] } },
    );
    expect(fieldLabel("body_format", bare)).toBe("Body format");
  });

  test("save validation uses only the active org's cached catalog", () => {
    queryClient.setQueryData(["integrationCatalog", "o_a"], catalog);
    queryClient.setQueryData(["integrationCatalog", "o_b"], []);
    const node = actionNode({
      integrationId: "gmail",
      actionId: "send_email",
      connectionId: "goc_1",
    });
    const ownErrors = (orgId?: string) =>
      getWorkflowErrors([node], orgId).filter((e) => e.startsWith("send:"));

    expect(ownErrors("o_a")).toEqual(["send: To is required."]);
    expect(ownErrors("o_b")).toEqual([]);
    expect(ownErrors("o_c")).toEqual([]);
  });
});
