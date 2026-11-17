import type { EmailInboxNode } from "./types";

const missing = (value: string): boolean => !value.trim();

export function validateEmailInboxNode(node: EmailInboxNode): Array<string> {
  const errors: Array<string> = [];
  const { label } = node.data;
  if (missing(node.data.emailClient)) {
    errors.push(`${label}: Email client is required.`);
  }
  if (node.data.credentialSource === "nango") {
    if (node.data.emailClient !== "gmail") {
      errors.push(`${label}: This email connection requires Gmail.`);
    }
    if (missing(node.data.connectionId ?? "")) {
      errors.push(`${label}: Gmail connection is required.`);
    }
    if (
      [
        node.data.connectionId,
        node.data.folder,
        node.data.sender,
        node.data.subject,
      ].some(
        (value) =>
          value && ["{{", "{%", "{#"].some((marker) => value.includes(marker)),
      )
    ) {
      errors.push(
        `${label}: Gmail connection, folder, sender, and subject must be literal.`,
      );
    }
  } else if (missing(node.data.credentialId)) {
    errors.push(`${label}: Email account is required.`);
  }
  return errors;
}
