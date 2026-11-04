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
    ],
    isLoading: false,
    isFetching: false,
  }),
}));

afterEach(cleanup);
const props = {
  provider: "powerbi" as const,
  connectionId: "test",
  disabled: false,
  onChange: vi.fn(),
};

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
