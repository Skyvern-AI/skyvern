// @vitest-environment jsdom
import { cleanup, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Settings } from "./Settings";

const scrollIntoViewMock = vi.fn();
const originalScrollIntoView = Element.prototype.scrollIntoView;

vi.mock("@/components/OnePasswordTokenForm", () => ({
  OnePasswordTokenForm: () => null,
}));
vi.mock("@/components/BitwardenCredentialForm", () => ({
  BitwardenCredentialForm: () => null,
}));
vi.mock("@/components/AzureClientSecretCredentialTokenForm", () => ({
  AzureClientSecretCredentialTokenForm: () => null,
}));
vi.mock("@/components/CustomCredentialServiceConfigForm", () => ({
  CustomCredentialServiceConfigForm: () => null,
}));
vi.mock("@/components/CustomLLMConfigForm", () => ({
  CustomLLMConfigForm: () => null,
}));
vi.mock("@/components/OrgLLMDefaultsCard", () => ({
  OrgLLMDefaultsCard: () => null,
}));
vi.mock("@/components/GoogleOAuthClientConfigForm", () => ({
  GoogleOAuthClientConfigForm: () => null,
}));
vi.mock("@/hooks/useVersionQuery", () => ({
  useVersionQuery: () => ({ data: undefined }),
}));

describe("Settings", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = scrollIntoViewMock;
  });

  afterEach(() => {
    cleanup();
    scrollIntoViewMock.mockClear();
    Element.prototype.scrollIntoView = originalScrollIntoView;
  });

  it("scrolls the linked settings section into view", async () => {
    render(
      <MemoryRouter initialEntries={["/settings#custom-llms"]}>
        <Settings />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(scrollIntoViewMock).toHaveBeenCalledWith({
        behavior: "smooth",
        block: "start",
      });
    });
  });
});
