import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PageSlotsProvider } from "@/store/PageSlots";
import { CredentialSourceSelector } from "./CredentialSourceSelector";

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>()),
  useGoogleOAuthCredentials: () => ({
    credentials: [
      {
        id: "goac_synthetic",
        credential_name: "Synthetic account",
        email_address: "SYNTHETIC_EMAIL_LABEL",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/gmail.readonly"],
      },
      ...[false, true].map((read) => ({
        id: read ? "goac_reply" : "goac_send",
        credential_name: read ? "SYNTHETIC_REPLY" : "SYNTHETIC_SEND_ONLY",
        state: "active",
        gmail_send_ready: true,
        scopes_granted: [
          "https://www.googleapis.com/auth/gmail.send",
          ...(read ? ["https://www.googleapis.com/auth/gmail.readonly"] : []),
        ],
      })),
    ],
    isLoading: false,
    isFetching: false,
  }),
}));

afterEach(cleanup);

it.each([false, true])(
  "handles a saved send-only reply account without editing disabled=%s",
  (disabled) => {
    const onChange = vi.fn();
    const saved = {
      provider: "gmail" as const,
      nodeId: "email",
      credentialId: "goac_send",
      credentialSource: "native" as const,
      connectionId: "",
      disabled,
      onChange,
    };
    const { rerender } = render(
      <CredentialSourceSelector {...saved} purpose="send" />,
    );
    expect(onChange).not.toHaveBeenCalled();
    rerender(<CredentialSourceSelector {...saved} purpose="reply" />);
    if (disabled) expect(onChange).not.toHaveBeenCalled();
    else expect(onChange).toHaveBeenCalledWith("native", "");
    rerender(
      <CredentialSourceSelector
        {...saved}
        credentialId={disabled ? "goac_send" : ""}
        purpose="reply"
      />,
    );
    expect(screen.getByRole("alert").textContent).toMatch(
      /read access.*reply/i,
    );
    expect(screen.queryByText(/cannot send email/)).toBeNull();
  },
);
const props = {
  provider: "powerbi" as const,
  connectionId: "test",
  disabled: false,
  onChange: vi.fn(),
};

it.each(["send", "reply"] as const)(
  "filters native Gmail accounts for %s",
  (purpose) => {
    const onChange = vi.fn();
    render(
      <CredentialSourceSelector
        {...props}
        provider="gmail"
        purpose={purpose}
        nodeId="email"
        credentialId=""
        credentialSource="native"
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Gmail account" }));
    expect(Boolean(screen.queryByText("SYNTHETIC_SEND_ONLY"))).toBe(
      purpose === "send",
    );
    expect(screen.queryByText("SYNTHETIC_EMAIL_LABEL")).toBeNull();
    fireEvent.click(screen.getByText("SYNTHETIC_REPLY"));
    expect(onChange).toHaveBeenCalledWith("native", "goac_reply");
  },
);

it("does not expose native Power BI controls without a Cloud slot", () => {
  render(<CredentialSourceSelector {...props} credentialSource="native" />);
  expect(screen.getByRole("status").textContent).toContain(
    "No native fallback",
  );
  expect(document.body.textContent).not.toMatch(/nango/i);
});

it("does not mount native controls for a Nango ID without a Cloud slot", () => {
  render(<CredentialSourceSelector {...props} credentialSource="nango" />);
  expect(screen.getByRole("status").textContent).toContain(
    "No native fallback",
  );
});

it("injects the Cloud source and ID pair through PageSlots", () => {
  render(
    <PageSlotsProvider
      value={{
        brokerCredentialSelector: (value) => (
          <span>
            {value.credentialSource}:{value.connectionId}
          </span>
        ),
      }}
    >
      <CredentialSourceSelector {...props} credentialSource="nango" />
    </PageSlotsProvider>,
  );
  expect(screen.getByText("nango:test")).toBeTruthy();
});

it.each([false, true])(
  "offers native accounts for unavailable Gmail, respecting disabled=%s",
  (disabled) => {
    const onChange = vi.fn();
    render(
      <CredentialSourceSelector
        {...props}
        provider="gmail"
        nodeId="email"
        credentialId=""
        credentialSource="nango"
        disabled={disabled}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("unavailable");
    expect(document.body.textContent).not.toMatch(/nango/i);
    const picker = screen.getByRole("combobox", { name: "Gmail account" });
    expect((picker as HTMLButtonElement).disabled).toBe(disabled);
    expect(onChange).not.toHaveBeenCalled();
    if (!disabled) {
      fireEvent.click(picker);
      fireEvent.click(screen.getByText("SYNTHETIC_EMAIL_LABEL"));
      expect(onChange).toHaveBeenCalledExactlyOnceWith(
        "native",
        "goac_synthetic",
      );
    }
  },
);
