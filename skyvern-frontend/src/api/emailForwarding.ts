import { isAxiosError } from "axios";

export type ForwardingAddress = {
  forwarding_address_id: string;
  organization_id: string;
  email_config_id: string;
  email_address: string;
  org_slug: string;
  match_mode: "dedicated" | "original_recipient";
  credential_id: string | null;
  sender_allowlist: string[] | null;
  status: "pending" | "active" | "disabled" | "revoked";
  last_received_at: string | null;
  has_pending_confirmation: boolean;
  created_at: string;
  modified_at: string;
};

export type ForwardedEmail = {
  inbound_email_id: string;
  from_address: string;
  subject: string;
  body_text: string;
  links: { url: string; text: string }[];
  category: "otp" | "provider_confirmation" | "dropped_spam" | "dropped_other";
  verdicts?: Record<string, string> | null;
  created_at: string;
};

export type MintForwardingAddress = {
  credential_id?: string;
  match_mode: ForwardingAddress["match_mode"];
  sender_allowlist?: string[];
};

export function isForwardingAccessDenied(error: unknown): boolean {
  if (
    !isAxiosError(error) ||
    error.response?.status !== 403 ||
    !error.config?.url?.startsWith("/email-forwarding/")
  ) {
    return false;
  }
  const detail: unknown = error.response.data?.detail;
  return (
    detail === "Email 2FA forwarding requires the Pro plan." ||
    (detail !== null &&
      typeof detail === "object" &&
      "error_code" in detail &&
      detail.error_code === "organization_admin_required")
  );
}

export function forwardingError(error: unknown): string {
  if (isAxiosError(error)) {
    const detail: unknown = error.response?.data?.detail;
    if (detail === "Credential already has a different TOTP binding") {
      return "This credential already has a two-factor method. To switch to forwarding, remove that method, save the credential, then open it again to generate an address.";
    }
    if (typeof detail === "string") return detail;
    if (
      detail &&
      typeof detail === "object" &&
      "message" in detail &&
      typeof detail.message === "string"
    ) {
      return detail.message;
    }
  }
  return "Could not update email forwarding. Try again.";
}

export function receivedAt(value: string | null): string {
  if (!value) return "No mail received";
  return new Date(
    /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`,
  ).toLocaleString();
}
