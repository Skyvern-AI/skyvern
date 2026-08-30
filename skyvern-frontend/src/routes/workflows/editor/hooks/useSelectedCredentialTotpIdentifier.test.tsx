// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import { useSelectedCredentialTotpIdentifier } from "./useSelectedCredentialTotpIdentifier";

const credentialsQuery = vi.hoisted(() => ({
  data: [] as Array<unknown>,
}));
const credentialQuery = vi.hoisted(() => ({
  data: undefined as unknown,
}));

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: credentialsQuery.data }),
}));
vi.mock("@/routes/workflows/hooks/useCredentialQuery", () => ({
  useCredentialQuery: () => ({ data: credentialQuery.data }),
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

const PASSWORD_CRED_ROTATED = {
  credential_id: "cred-3",
  credential_type: "password" as const,
  credential: {
    username: "carol",
    password: "*",
    totp_identifier: "carol@example.com",
  },
};

beforeEach(() => {
  credentialsQuery.data = [];
  credentialQuery.data = undefined;
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

  test("reflects every rotated credential's totp_identifier, not just the first", () => {
    credentialsQuery.data = [PASSWORD_CRED, PASSWORD_CRED_ROTATED];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "my_cred",
          parameterType: "credential",
          credentialId: "cred-1",
          credentialIds: ["cred-1", "cred-3"],
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("my_cred"),
    );
    expect(result.current).toBe("alice@example.com, carol@example.com");
  });

  test("de-dupes identical totp_identifiers shared across rotated credentials", () => {
    credentialsQuery.data = [
      PASSWORD_CRED,
      { ...PASSWORD_CRED_ROTATED, credential: PASSWORD_CRED.credential },
    ];
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "my_cred",
          parameterType: "credential",
          credentialId: "cred-1",
          credentialIds: ["cred-1", "cred-3"],
        },
      ],
    });
    const { result } = renderHook(() =>
      useSelectedCredentialTotpIdentifier("my_cred"),
    );
    expect(result.current).toBe("alice@example.com");
  });

  test("resolves the identifier from the detail query when the first page omits the credential", () => {
    credentialQuery.data = PASSWORD_CRED;
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
});
