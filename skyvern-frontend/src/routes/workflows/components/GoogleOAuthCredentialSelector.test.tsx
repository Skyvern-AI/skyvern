// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { GoogleOAuthCredential } from "@/api/types";

const mocks = vi.hoisted(() => ({
  useGoogleOAuthCredentials: vi.fn(),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mocks.useGoogleOAuthCredentials,
  };
});

import { GoogleOAuthCredentialSelector } from "./GoogleOAuthCredentialSelector";

const driveScope = "https://www.googleapis.com/auth/drive";

function googleCredential(id: string): GoogleOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: "Primary Drive",
    state: "active",
    scopes_granted: [driveScope],
    email_address: `${id}@gmail.test`,
    created_at: "2026-08-11T00:00:00Z",
    modified_at: "2026-08-11T00:00:00Z",
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useGoogleOAuthCredentials.mockReturnValue({
    credentials: [googleCredential("goac-connected")],
    isLoading: false,
    isFetching: false,
    error: null,
  });
});

afterEach(() => {
  cleanup();
});

describe("GoogleOAuthCredentialSelector", () => {
  test("keeps an optional credential empty when connected accounts exist", () => {
    const onChange = vi.fn();

    render(
      <GoogleOAuthCredentialSelector
        nodeId="d1"
        value=""
        onChange={onChange}
        requiredScopes={[driveScope]}
        optional
      />,
    );

    expect(onChange).not.toHaveBeenCalled();
  });

  test("can clear an optional selected account", () => {
    const onChange = vi.fn();

    render(
      <GoogleOAuthCredentialSelector
        nodeId="d1"
        value="goac-connected"
        onChange={onChange}
        requiredScopes={[driveScope]}
        optional
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("No Google account"));

    expect(onChange).toHaveBeenCalledWith("");
  });
});
