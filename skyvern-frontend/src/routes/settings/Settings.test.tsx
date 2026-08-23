// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { persistRuntimeApiKey, clearRuntimeApiKey } from "@/util/env";
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
    clearRuntimeApiKey();
    window.sessionStorage.clear();
    scrollIntoViewMock.mockClear();
    Element.prototype.scrollIntoView = originalScrollIntoView;
  });

  it("shows the session token as soon as it is minted", async () => {
    // The page renders before the first mint lands, so a value read once at render never updates.
    const { getByDisplayValue } = render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Settings />
      </MemoryRouter>,
    );
    const masked = getByDisplayValue("**** **** **** ****");
    const revealButton = within(
      masked.parentElement as HTMLElement,
    ).getAllByRole("button")[0]!;
    fireEvent.click(revealButton);
    getByDisplayValue("Waiting for a browser session token");

    act(() => {
      persistRuntimeApiKey(
        "minted-session-canary",
        Math.floor(Date.now() / 1000) + 3600,
      );
    });

    await waitFor(() => {
      expect(getByDisplayValue("minted-session-canary")).toBeTruthy();
    });
  });

  it("says the credential on screen is short-lived", async () => {
    persistRuntimeApiKey(
      "short-lived-canary",
      Math.floor(Date.now() / 1000) + 3600,
    );

    const { findByText } = render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Settings />
      </MemoryRouter>,
    );

    // Copying it into an SDK or a schedule silently breaks an hour later.
    expect(await findByText(/Short-lived browser session token/)).toBeTruthy();
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
