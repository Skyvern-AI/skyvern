// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import { useSelectedCredentialTotpIdentifier } from "./useSelectedCredentialTotpIdentifier";

const credentialsQuery = vi.hoisted(() => ({
  data: [] as Array<unknown>,
}));

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: credentialsQuery.data }),
}));

const PASSWORD_CRED = {
  credential_id: "cred-1",
  credential_type: "password" as const,
  credential: {
    username: "alice",
    password: "*",
    totp_identifier: "alice@example.com",
  },
};

const PASSWORD_CRED_NO_TOTP = {
  credential_id: "cred-2",
  credential_type: "password" as const,
  credential: { username: "bob", password: "*", totp_identifier: null },
};

beforeEach(() => {
  credentialsQuery.data = [];
  useWorkflowParametersStore.setState({ parameters: [] });
});

describe("useSelectedCredentialTotpIdentifier", () => {
  test("returns null when no parameterKey is provided", () => {
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier(undefined),
    );
    expect(result.current).toBeNull();
  });

  test("resolves via a Skyvern credential parameter pointing at a credential with a stored totp_identifier", () => {
    credentialsQuery.data = [PASSWORD_CRED];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "my_cred",
          parameterType: "credential",
          credentialId: "cred-1",
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("my_cred"),
    );
    expect(result.current).toBe("alice@example.com");
  });

  test("returns null when the matched credential has no stored totp_identifier", () => {
    credentialsQuery.data = [PASSWORD_CRED_NO_TOTP];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "my_cred",
          parameterType: "credential",
          credentialId: "cred-2",
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("my_cred"),
    );
    expect(result.current).toBeNull();
  });

  test("falls back to a workflow parameter whose dataType is credential_id and defaultValue points at a stored credential", () => {
    credentialsQuery.data = [PASSWORD_CRED];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "my_cred",
          parameterType: "workflow",
          dataType: "credential_id",
          defaultValue: "cred-1",
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("my_cred"),
    );
    expect(result.current).toBe("alice@example.com");
  });

  test("returns null when the parameter key matches nothing", () => {
    credentialsQuery.data = [PASSWORD_CRED];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "other_key",
          parameterType: "credential",
          credentialId: "cred-1",
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("missing_key"),
    );
    expect(result.current).toBeNull();
  });
});
