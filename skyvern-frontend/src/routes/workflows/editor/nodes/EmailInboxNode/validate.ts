import type { EmailInboxNode } from "./types";

const missing = (value: string): boolean => !value.trim();

export function validateEmailInboxNode(node: EmailInboxNode): Array<string> {
  const errors: Array<string> = [];
  const { label } = node.data;
  if (missing(node.data.emailClient)) {
    errors.push(`${label}: Email client is required.`);
  }
  if (missing(node.data.credentialId)) {
    errors.push(`${label}: Email account is required.`);
  }
  return errors;
}
