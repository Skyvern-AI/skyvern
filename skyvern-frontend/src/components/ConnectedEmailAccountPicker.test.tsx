// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  GoogleOAuthCredential,
  MicrosoftOAuthCredential,
} from "@/api/types";

const mocks = vi.hoisted(() => ({
  useGoogleOAuthCredentials: vi.fn(),
  useMicrosoftOAuthCredentials: vi.fn(),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mocks.useGoogleOAuthCredentials,
  };
});

vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useMicrosoftOAuthCredentials")>();
  return {
    ...actual,
    useMicrosoftOAuthCredentials: mocks.useMicrosoftOAuthCredentials,
  };
});

import { ConnectedEmailAccountPicker } from "./ConnectedEmailAccountPicker";

const gmailScope = "https://www.googleapis.com/auth/gmail.readonly";

function googleCredential(
  id: string,
  overrides: Partial<GoogleOAuthCredential> = {},
): GoogleOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: "Default",
    state: "active",
    scopes_granted: [gmailScope],
    email_address: `${id}@gmail.test`,
    created_at: "2026-07-30T00:00:00Z",
    modified_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
}

function microsoftCredential(
  id: string,
  overrides: Partial<MicrosoftOAuthCredential> = {},
): MicrosoftOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: "Default",
    state: "active",
    scopes_granted: ["Mail.Read"],
    email_address: `${id}@outlook.test`,
    created_at: "2026-07-30T00:00:00Z",
    modified_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
}

function installHooks({
  google = [],
  microsoft = [],
  googleIsLoading = false,
  microsoftIsLoading = false,
  googleError = null,
  microsoftError = null,
}: {
  google?: GoogleOAuthCredential[];
  microsoft?: MicrosoftOAuthCredential[];
  googleIsLoading?: boolean;
  microsoftIsLoading?: boolean;
  googleError?: Error | null;
  microsoftError?: Error | null;
} = {}) {
  mocks.useGoogleOAuthCredentials.mockReturnValue({
    credentials: google,
    isLoading: googleIsLoading,
    isFetching: false,
    error: googleError,
  });
  mocks.useMicrosoftOAuthCredentials.mockReturnValue({
    credentials: microsoft,
    isLoading: microsoftIsLoading,
    isFetching: false,
    error: microsoftError,
  });
}

function picker(value = "", onChange = vi.fn()) {
  return (
    <ConnectedEmailAccountPicker
      value={value}
      onChange={onChange}
      renderCustomInput={({ value: customValue, onChange: onCustomChange }) => (
        <input
          aria-label="Custom 2FA identifier"
          value={customValue}
          onChange={(event) => onCustomChange(event.target.value)}
        />
      )}
    />
  );
}

function renderPicker(value = "", onChange = vi.fn()) {
  render(picker(value, onChange));
  return onChange;
}

beforeEach(() => {
  vi.clearAllMocks();
  installHooks();
});

afterEach(() => {
  cleanup();
});

describe("ConnectedEmailAccountPicker", () => {
  it("shows only active, mail-scoped accounts with resolved email addresses", () => {
    installHooks({
      google: [
        googleCredential("gmail-active"),
        googleCredential("gmail-inactive", { state: "error" }),
        googleCredential("gmail-unscoped", {
          scopes_granted: ["https://www.googleapis.com/auth/drive"],
        }),
        googleCredential("gmail-unresolved", { email_address: null }),
      ],
      microsoft: [
        microsoftCredential("outlook-active", {
          credential_name: "Finance",
        }),
        microsoftCredential("outlook-inactive", { state: "error" }),
        microsoftCredential("outlook-unscoped", {
          scopes_granted: ["Files.Read"],
        }),
      ],
    });

    renderPicker();
    fireEvent.click(
      screen.getByRole("combobox", { name: "Connected email account" }),
    );

    expect(screen.getByText("gmail-active@gmail.test")).toBeTruthy();
    expect(screen.getByText("outlook-active@outlook.test")).toBeTruthy();
    expect(screen.getByText("Gmail")).toBeTruthy();
    expect(screen.getByText("Outlook · Finance")).toBeTruthy();
    expect(screen.queryByText("gmail-inactive@gmail.test")).toBeNull();
    expect(screen.queryByText("gmail-unscoped@gmail.test")).toBeNull();
    expect(screen.queryByText("outlook-inactive@outlook.test")).toBeNull();
    expect(screen.queryByText("outlook-unscoped@outlook.test")).toBeNull();
    expect(mocks.useGoogleOAuthCredentials).toHaveBeenCalledWith({
      includeEmail: true,
    });
    expect(mocks.useMicrosoftOAuthCredentials).toHaveBeenCalledWith({
      includeEmail: true,
    });
  });

  it("writes the selected account email address", () => {
    installHooks({
      google: [googleCredential("selected")],
    });
    const onChange = renderPicker();

    fireEvent.click(
      screen.getByRole("combobox", { name: "Connected email account" }),
    );
    fireEvent.click(screen.getByText("selected@gmail.test"));

    expect(onChange).toHaveBeenCalledWith("selected@gmail.test");
  });

  it("toggles to the caller-provided custom input", () => {
    installHooks({
      google: [googleCredential("connected")],
    });
    renderPicker();

    fireEvent.click(screen.getByRole("button", { name: "Enter manually" }));

    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Choose connected account" }),
    ).toBeTruthy();
  });

  it("keeps a non-option custom value visible when dropdown mode is requested", () => {
    installHooks({
      google: [googleCredential("connected")],
    });
    renderPicker("custom@example.test");

    fireEvent.click(
      screen.getByRole("button", { name: "Choose connected account" }),
    );

    expect(
      (screen.getByLabelText("Custom 2FA identifier") as HTMLInputElement)
        .value,
    ).toBe("custom@example.test");
    expect(
      screen.getByRole("button", { name: "Choose connected account" }),
    ).toBeTruthy();
  });

  it("starts a Jinja value in custom mode", () => {
    installHooks({
      google: [googleCredential("connected")],
    });
    renderPicker("{{ inbox_email }}");

    expect(
      (screen.getByLabelText("Custom 2FA identifier") as HTMLInputElement)
        .value,
    ).toBe("{{ inbox_email }}");
  });

  it("stays in custom mode when the custom value is cleared", () => {
    installHooks({
      google: [googleCredential("connected")],
    });
    const onChange = vi.fn();
    const { rerender } = render(
      <ConnectedEmailAccountPicker
        value="{{ inbox_email }}"
        onChange={onChange}
        renderCustomInput={({ value, onChange: onCustomChange }) => (
          <input
            aria-label="Custom 2FA identifier"
            value={value}
            onChange={(event) => onCustomChange(event.target.value)}
          />
        )}
      />,
    );

    fireEvent.change(screen.getByLabelText("Custom 2FA identifier"), {
      target: { value: "" },
    });
    expect(onChange).toHaveBeenCalledWith("");

    rerender(
      <ConnectedEmailAccountPicker
        value=""
        onChange={onChange}
        renderCustomInput={({ value, onChange: onCustomChange }) => (
          <input
            aria-label="Custom 2FA identifier"
            value={value}
            onChange={(event) => onCustomChange(event.target.value)}
          />
        )}
      />,
    );

    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Choose connected account" }),
    ).toBeTruthy();
  });

  it("links to integrations when there are no connected mail accounts", () => {
    installHooks({
      googleIsLoading: true,
      microsoftIsLoading: true,
    });
    const { rerender } = render(picker());

    installHooks();
    rerender(picker());

    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();
    const link = screen.getByRole("link", {
      name: "Connect Gmail or Outlook to pick an account",
    });
    expect(link.getAttribute("href")).toBe("/integrations");
  });

  it("latches dropdown mode after slow queries resolve to the saved account", () => {
    installHooks({
      googleIsLoading: true,
      microsoftIsLoading: true,
    });
    const { rerender } = render(picker("saved@gmail.test"));

    expect(screen.getByText("Loading connected accounts...")).toBeTruthy();

    installHooks({
      google: [
        googleCredential("saved", { email_address: "saved@gmail.test" }),
      ],
    });
    rerender(picker("saved@gmail.test"));

    expect(screen.queryByLabelText("Custom 2FA identifier")).toBeNull();
    expect(screen.getByText("saved@gmail.test")).toBeTruthy();
  });

  it("switches a settled dropdown to custom when the controlled value no longer matches", () => {
    installHooks({
      google: [
        googleCredential("saved", { email_address: "saved@gmail.test" }),
      ],
    });
    const { rerender } = render(picker("saved@gmail.test"));

    expect(screen.queryByLabelText("Custom 2FA identifier")).toBeNull();

    rerender(picker("seeded-username"));

    expect(
      (screen.getByLabelText("Custom 2FA identifier") as HTMLInputElement)
        .value,
    ).toBe("seeded-username");
    expect(
      screen.getByRole("button", { name: "Choose connected account" }),
    ).toBeTruthy();
  });

  it("switches a settled dropdown to custom when its matching option disappears", () => {
    installHooks({
      google: [
        googleCredential("saved", { email_address: "saved@gmail.test" }),
      ],
    });
    const { rerender } = render(picker("saved@gmail.test"));

    expect(screen.queryByLabelText("Custom 2FA identifier")).toBeNull();

    installHooks();
    rerender(picker("saved@gmail.test"));

    expect(
      (screen.getByLabelText("Custom 2FA identifier") as HTMLInputElement)
        .value,
    ).toBe("saved@gmail.test");
    expect(
      screen.getByRole("button", { name: "Choose connected account" }),
    ).toBeTruthy();
  });

  it("respects a user toggle while connected accounts are loading", () => {
    installHooks({
      googleIsLoading: true,
      microsoftIsLoading: true,
    });
    const { rerender } = render(picker());

    fireEvent.click(screen.getByRole("button", { name: "Enter manually" }));
    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();

    installHooks({ google: [googleCredential("connected")] });
    rerender(picker());

    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();
  });

  it("shows a neutral manual-entry hint when account loading fails", () => {
    installHooks({ googleError: new Error("request failed") });

    renderPicker();

    expect(screen.getByLabelText("Custom 2FA identifier")).toBeTruthy();
    expect(
      screen.getByText(
        "Couldn't load connected accounts — enter the address manually",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
