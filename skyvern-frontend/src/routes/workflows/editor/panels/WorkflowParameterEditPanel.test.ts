import { describe, expect, it } from "vitest";
import { ParametersState } from "../types";
import {
  detectInitialCredentialDataType,
  detectInitialCredentialSource,
  detectInitialParameterTypeSelection,
  header,
} from "./WorkflowParameterEditPanel.helpers";

type Parameter = ParametersState[number];

const workflowString: Parameter = {
  key: "name",
  parameterType: "workflow",
  dataType: "string",
  description: null,
  defaultValue: null,
};

const workflowInteger: Parameter = {
  ...workflowString,
  dataType: "integer",
};

const context: Parameter = {
  key: "ctx",
  parameterType: "context",
  sourceParameterKey: "name",
};

const bitwardenLogin: Parameter = {
  key: "login",
  parameterType: "credential",
  collectionId: "collection-id",
  itemId: "item-id",
  urlParameterKey: null,
};

const skyvernCredential: Parameter = {
  key: "skyvern_cred",
  parameterType: "credential",
  credentialId: "cred-123",
};

const azureCredential: Parameter = {
  key: "azure_cred",
  parameterType: "credential",
  vaultName: "vault",
  usernameKey: "u",
  passwordKey: "p",
  totpSecretKey: null,
};

const onepasswordCredential: Parameter = {
  key: "op",
  parameterType: "onepassword",
  vaultId: "v",
  itemId: "i",
};

const secretCredential: Parameter = {
  key: "secret",
  parameterType: "secret",
  identityKey: "id",
  identityFields: ["ssn"],
  collectionId: "collection-id",
};

const creditCardCredential: Parameter = {
  key: "cc",
  parameterType: "creditCardData",
  itemId: "item-id",
  collectionId: "collection-id",
};

describe("detectInitialParameterTypeSelection", () => {
  it("returns null when there are no initial values", () => {
    expect(detectInitialParameterTypeSelection(undefined)).toBeNull();
  });

  it("returns the dataType for workflow inputs", () => {
    expect(detectInitialParameterTypeSelection(workflowString)).toBe("string");
    expect(detectInitialParameterTypeSelection(workflowInteger)).toBe(
      "integer",
    );
  });

  it("collapses every credential variant to 'credential'", () => {
    expect(detectInitialParameterTypeSelection(bitwardenLogin)).toBe(
      "credential",
    );
    expect(detectInitialParameterTypeSelection(skyvernCredential)).toBe(
      "credential",
    );
    expect(detectInitialParameterTypeSelection(azureCredential)).toBe(
      "credential",
    );
    expect(detectInitialParameterTypeSelection(onepasswordCredential)).toBe(
      "credential",
    );
    expect(detectInitialParameterTypeSelection(secretCredential)).toBe(
      "credential",
    );
    expect(detectInitialParameterTypeSelection(creditCardCredential)).toBe(
      "credential",
    );
  });

  it("returns null for context parameters", () => {
    expect(detectInitialParameterTypeSelection(context)).toBeNull();
  });
});

describe("header", () => {
  it("uses 'Add Input' as the unified add-mode title", () => {
    expect(header("workflow", false)).toBe("Add Input");
  });

  it("uses 'Edit Input' when editing any non-context parameter", () => {
    expect(header("workflow", true)).toBe("Edit Input");
  });

  it("preserves dedicated context titles for the context entry-point type", () => {
    expect(header("context", false)).toBe("Add Context Input");
    expect(header("context", true)).toBe("Edit Context Input");
  });
});

describe("detectInitialCredentialDataType", () => {
  it("defaults to 'password' when no initial values are provided", () => {
    expect(detectInitialCredentialDataType(undefined)).toBe("password");
  });

  it("maps secret parameters to 'secret'", () => {
    expect(detectInitialCredentialDataType(secretCredential)).toBe("secret");
  });

  it("maps creditCardData parameters to 'creditCard'", () => {
    expect(detectInitialCredentialDataType(creditCardCredential)).toBe(
      "creditCard",
    );
  });

  it("falls back to 'password' for other credential variants", () => {
    expect(detectInitialCredentialDataType(bitwardenLogin)).toBe("password");
    expect(detectInitialCredentialDataType(skyvernCredential)).toBe("password");
    expect(detectInitialCredentialDataType(onepasswordCredential)).toBe(
      "password",
    );
    expect(detectInitialCredentialDataType(azureCredential)).toBe("password");
  });
});

describe("detectInitialCredentialSource", () => {
  it("defaults to 'skyvern' on cloud and 'bitwarden' off-cloud when there are no initial values", () => {
    expect(detectInitialCredentialSource(undefined, true)).toBe("skyvern");
    expect(detectInitialCredentialSource(undefined, false)).toBe("bitwarden");
  });

  it("treats secret and creditCardData as Bitwarden", () => {
    expect(detectInitialCredentialSource(secretCredential, true)).toBe(
      "bitwarden",
    );
    expect(detectInitialCredentialSource(creditCardCredential, true)).toBe(
      "bitwarden",
    );
  });

  it("treats onepassword parameters as 1Password", () => {
    expect(detectInitialCredentialSource(onepasswordCredential, true)).toBe(
      "onepassword",
    );
  });

  it("disambiguates credential-typed parameters by their shape", () => {
    expect(detectInitialCredentialSource(bitwardenLogin, true)).toBe(
      "bitwarden",
    );
    expect(detectInitialCredentialSource(skyvernCredential, true)).toBe(
      "skyvern",
    );
    expect(detectInitialCredentialSource(azureCredential, true)).toBe(
      "azurevault",
    );
  });
});
