import { describe, expect, it } from "vitest";
import type { GoogleOAuthCredential } from "@/api/types";
import {
  getGoogleOAuthCredentialScopesGranted,
  getGoogleOAuthCredentialScopesRequested,
  matchesGoogleOAuthIntegrationScopes,
  normalizeGoogleOAuthScopes,
} from "./useGoogleOAuthCredentials";

const baseCredential: GoogleOAuthCredential = {
  id: "credential_1",
  organization_id: "org_1",
  credential_name: "Google",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
};

describe("Google OAuth credential scope helpers", () => {
  it("normalizes array and string scope payloads", () => {
    expect(normalizeGoogleOAuthScopes(["scope:a", "scope:b"])).toEqual([
      "scope:a",
      "scope:b",
    ]);
    expect(normalizeGoogleOAuthScopes("scope:a scope:b,scope:c")).toEqual([
      "scope:a",
      "scope:b",
      "scope:c",
    ]);
  });

  it("falls back to legacy string scopes when scopes_granted is absent", () => {
    expect(
      getGoogleOAuthCredentialScopesGranted({
        ...baseCredential,
        scopes: "https://www.googleapis.com/auth/gmail.readonly openid",
      }),
    ).toEqual(["https://www.googleapis.com/auth/gmail.readonly", "openid"]);
  });

  it("prefers scopes_granted over legacy scopes", () => {
    expect(
      getGoogleOAuthCredentialScopesGranted({
        ...baseCredential,
        scopes_granted: ["new:scope"],
        scopes: "legacy:scope",
      }),
    ).toEqual(["new:scope"]);
  });

  it("normalizes requested scopes", () => {
    expect(
      getGoogleOAuthCredentialScopesRequested({
        ...baseCredential,
        scopes_requested: "requested:a requested:b",
      }),
    ).toEqual(["requested:a", "requested:b"]);
  });

  it("matches integrations by requested scopes before cumulative granted scopes", () => {
    const credential = {
      ...baseCredential,
      scopes_requested: ["gmail"],
      scopes_granted: ["gmail", "sheets"],
    };

    expect(matchesGoogleOAuthIntegrationScopes(credential, ["gmail"])).toBe(
      true,
    );
    expect(matchesGoogleOAuthIntegrationScopes(credential, ["sheets"])).toBe(
      false,
    );
  });

  it("falls back to granted scopes when requested scopes are absent", () => {
    expect(
      matchesGoogleOAuthIntegrationScopes(
        {
          ...baseCredential,
          scopes_granted: ["sheets"],
        },
        ["sheets"],
      ),
    ).toBe(true);
  });
});
