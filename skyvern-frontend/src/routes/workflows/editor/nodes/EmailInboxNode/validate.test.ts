import { describe, expect, test } from "vitest";
import { emailInboxNodeDefaultData, type EmailInboxNode } from "./types";
import { validateEmailInboxNode } from "./validate";

describe("Email Inbox validation", () => {
  test("keeps native validation and blocks Nango settings that cannot run", () => {
    const node: EmailInboxNode = {
      id: "email",
      type: "emailInbox",
      position: { x: 0, y: 0 },
      data: {
        ...emailInboxNodeDefaultData,
        label: "Read mail",
        credentialId: "native_test",
      },
    };
    expect(validateEmailInboxNode(node)).toEqual([]);
    node.data = {
      ...node.data,
      credentialSource: "nango",
      credentialId: "",
      connectionId: "nc_synthetic",
    };
    expect(validateEmailInboxNode(node)).toEqual([]);
    node.data = { ...node.data, downloadAttachments: true };
    expect(validateEmailInboxNode(node)).toEqual([]);
    node.data = {
      ...node.data,
      connectionId: "",
      credentialId: "native_test",
      folder: "{{ folder }}",
    };
    expect(validateEmailInboxNode(node)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("connection is required"),
        expect.stringContaining("must be literal"),
      ]),
    );
    expect(validateEmailInboxNode(node).join(" ")).not.toMatch(/nango/i);
  });
});
