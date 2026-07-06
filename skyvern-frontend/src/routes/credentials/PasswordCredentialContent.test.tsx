// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CredentialAuthenticatorSupportProvider } from "./CredentialAuthenticatorSupportContext";
import { PasswordCredentialContent } from "./PasswordCredentialContent";

type Values = {
  name: string;
  username: string;
  password: string;
  totp: string;
  totp_type: "authenticator" | "email" | "text" | "none";
  totp_identifier: string;
};

const INITIAL_VALUES: Values = {
  name: "",
  username: "",
  password: "",
  totp: "",
  totp_type: "none",
  totp_identifier: "",
};

const SAVED_VALUES: Values = {
  name: "Example Login",
  username: "user@example.com",
  password: "",
  totp: "",
  totp_type: "email",
  totp_identifier: "saved-totp-id@example.com",
};

const ENTERPRISE_APPS = {
  label: "Enterprise QR support",
  apps: ["Enterprise Authenticator A", "Enterprise Authenticator B"],
  contactUrl: "https://www.skyvern.com/contact",
  qrCodeTypes: [
    {
      id: "example",
      label: "Enterprise Authenticator A",
    },
    {
      id: "opaque",
      label: "Enterprise Authenticator B",
    },
  ],
  inferQrCodeType: (value: string) => {
    if (value.startsWith("example-authenticator://")) {
      return "example";
    }
    if (value.startsWith("opaque-authenticator://")) {
      return "opaque";
    }
    return null;
  },
  vendorLabels: {
    opaque: "Enterprise Authenticator B",
  },
  description:
    "Scan the QR as usual - Skyvern detects these setup codes automatically.",
};

// Mirrors CredentialsModal: starts the form at INITIAL_VALUES, then after mount
// applies the loaded credential via setPasswordCredentialValues — same flow
// that produced SKY-9864.
function ModalLikeHarness({
  appliedValues,
  onChangeSpy,
}: {
  appliedValues: Values | null;
  onChangeSpy: (next: Values) => void;
}) {
  const [values, setValues] = useState<Values>(INITIAL_VALUES);
  useEffect(() => {
    if (appliedValues) {
      setValues(appliedValues);
    }
  }, [appliedValues]);
  return (
    <MemoryRouter>
      <PasswordCredentialContent
        values={values}
        onChange={(next) => {
          onChangeSpy(next);
          setValues(next);
        }}
        editMode
        editingGroups={{ name: false, values: false }}
      />
    </MemoryRouter>
  );
}

describe("PasswordCredentialContent — edit-mode hydration (SKY-9864 regression)", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("preserves saved totp_identifier when modal hydrates an email-TOTP credential into a form that started at PASSWORD_CREDENTIAL_INITIAL_VALUES", async () => {
    // Reproduces the bug:
    //   1. CredentialsModal initially renders PasswordCredentialContent with
    //      PASSWORD_CREDENTIAL_INITIAL_VALUES (totp_type="none").
    //   2. The modal's useEffect then setPasswordCredentialValues({...cred...})
    //      with the saved cred — totp_type="email" plus the saved
    //      totp_identifier (e.g. "saved-totp-id@example.com").
    //   3. The sync useEffect inside PasswordCredentialContent flips totpMethod
    //      from default "authenticator" to "email", which triggers the
    //      auto-fill useEffect with methodChanged=true and silently overwrites
    //      the saved totp_identifier with username.
    const onChangeSpy = vi.fn();

    const { rerender } = render(
      <ModalLikeHarness appliedValues={null} onChangeSpy={onChangeSpy} />,
    );

    // Now simulate the modal applying the loaded credential's values via
    // setPasswordCredentialValues (a single render with a non-null appliedValues
    // prop, then the inner useEffect calls setValues).
    await act(async () => {
      rerender(
        <ModalLikeHarness
          appliedValues={SAVED_VALUES}
          onChangeSpy={onChangeSpy}
        />,
      );
    });

    // The component must NOT have called onChange with totp_identifier set to
    // the username — that would silently corrupt the saved value on next save.
    const corruptingCalls = onChangeSpy.mock.calls.filter((call) => {
      const next = call[0] as Values;
      return (
        next.totp_identifier === SAVED_VALUES.username &&
        next.totp_identifier !== SAVED_VALUES.totp_identifier
      );
    });
    expect(corruptingCalls).toEqual([]);
  });

  it("auto-fills totp_identifier with the username when the user explicitly switches the method to Email and the identifier is blank", async () => {
    const onChangeSpy = vi.fn();

    render(
      <ModalLikeHarness
        appliedValues={{
          name: "New cred",
          username: "new-user@example.com",
          password: "",
          totp: "",
          totp_type: "none",
          totp_identifier: "",
        }}
        onChangeSpy={onChangeSpy}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Email"));
    });

    const autoFillCall = onChangeSpy.mock.calls.find((call) => {
      const next = call[0] as Values;
      return (
        next.totp_type === "email" &&
        next.totp_identifier === "new-user@example.com"
      );
    });
    expect(autoFillCall).toBeTruthy();
  });

  it("marks Authenticator App as the selected 2FA method when a new credential opens the 2FA section", async () => {
    const onChangeSpy = vi.fn();

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });

    const authenticatorCall = onChangeSpy.mock.calls.find((call) => {
      const next = call[0] as Values;
      return next.totp_type === "authenticator";
    });
    expect(authenticatorCall).toBeTruthy();
  });

  it("keeps Authenticator App selected when the user types a key without clicking the method tile", async () => {
    const onChangeSpy = vi.fn();

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    fireEvent.change(screen.getByPlaceholderText("e.g. JBSWY3DPEHPK3PXP"), {
      target: { value: "JBSWY3DPEHPK3PXP" },
    });

    const typedCall = onChangeSpy.mock.calls.find((call) => {
      const next = call[0] as Values;
      return (
        next.totp_type === "authenticator" && next.totp === "JBSWY3DPEHPK3PXP"
      );
    });
    expect(typedCall).toBeTruthy();
  });

  it("imports an otpauth URI from an uploaded QR code image", async () => {
    const onChangeSpy = vi.fn();
    const imageBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    class MockBarcodeDetector {
      detect = vi.fn().mockResolvedValue([
        {
          rawValue:
            "otpauth://totp/user@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Example",
        },
      ]);
    }
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(imageBitmap));
    vi.stubGlobal("BarcodeDetector", MockBarcodeDetector);

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    fireEvent.change(screen.getByLabelText("Upload QR code image"), {
      target: {
        files: [new File(["qr"], "totp.png", { type: "image/png" })],
      },
    });

    await waitFor(() => {
      const qrCall = onChangeSpy.mock.calls.find((call) => {
        const next = call[0] as Values;
        return (
          next.totp_type === "authenticator" &&
          next.totp.startsWith("otpauth://totp/")
        );
      });
      expect(qrCall).toBeTruthy();
    });
    expect(
      screen
        .getByRole("button", { name: /Google Authenticator/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen.getByTestId("authenticator-qr-type-detection").textContent,
    ).toBe("Detected from QR");
    expect(imageBitmap.close).toHaveBeenCalled();
  });

  it("imports a non-otpauth QR payload for server-side validation", async () => {
    const onChangeSpy = vi.fn();
    const imageBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    class MockBarcodeDetector {
      detect = vi.fn().mockResolvedValue([
        {
          rawValue: "custom-authenticator://activate?payload=opaque",
        },
      ]);
    }
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(imageBitmap));
    vi.stubGlobal("BarcodeDetector", MockBarcodeDetector);

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    fireEvent.change(screen.getByLabelText("Upload QR code image"), {
      target: {
        files: [new File(["qr"], "totp.png", { type: "image/png" })],
      },
    });

    await waitFor(() => {
      const qrCall = onChangeSpy.mock.calls.find((call) => {
        const next = call[0] as Values;
        return (
          next.totp_type === "authenticator" &&
          next.totp === "custom-authenticator://activate?payload=opaque"
        );
      });
      expect(qrCall).toBeTruthy();
    });
    expect(
      screen
        .getByRole("button", { name: /Google Authenticator/ })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.queryByText("Detected from QR")).toBeNull();
    expect(imageBitmap.close).toHaveBeenCalled();
  });

  it("shows an inline fallback when the browser cannot scan QR codes", async () => {
    const onChangeSpy = vi.fn();
    vi.stubGlobal("BarcodeDetector", undefined);

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    fireEvent.change(screen.getByLabelText("Upload QR code image"), {
      target: {
        files: [new File(["qr"], "totp.png", { type: "image/png" })],
      },
    });

    await waitFor(() => {
      expect(
        screen.getByText(
          "QR scanning is not supported by this browser. Paste the setup key or otpauth:// URI instead.",
        ),
      ).toBeTruthy();
    });
  });

  it("shows the QR fallback when the browser rejects QR detection", async () => {
    const onChangeSpy = vi.fn();
    const createImageBitmapMock = vi.fn();
    class MockBarcodeDetector {
      constructor() {
        throw new TypeError("Unsupported barcode format");
      }

      detect = vi.fn();
    }
    vi.stubGlobal("createImageBitmap", createImageBitmapMock);
    vi.stubGlobal("BarcodeDetector", MockBarcodeDetector);

    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "New cred",
            username: "new-user@example.com",
            password: "password",
            totp: "",
            totp_type: "none",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    fireEvent.change(screen.getByLabelText("Upload QR code image"), {
      target: {
        files: [new File(["qr"], "totp.png", { type: "image/png" })],
      },
    });

    await waitFor(() => {
      expect(
        screen.getByText(
          "QR scanning is not supported by this browser. Paste the setup key or otpauth:// URI instead.",
        ),
      ).toBeTruthy();
    });
    expect(createImageBitmapMock).not.toHaveBeenCalled();
  });

  it("preserves an intentionally-empty totp_identifier when hydrating a saved email-TOTP credential", async () => {
    // Regression for the empty-string false-positive in the username-rename
    // follow-on path: at mount, prevUsername="" and identifier="". After
    // hydration, identifier is still "" — same value as prevUsername, so
    // identifierMatchedPrevUsername=true. Without the prevUsername!=="" guard,
    // the effect silently fires updateValues({ totp_identifier: username }),
    // overwriting a value the user deliberately saved as blank.
    const onChangeSpy = vi.fn();

    const { rerender } = render(
      <ModalLikeHarness appliedValues={null} onChangeSpy={onChangeSpy} />,
    );

    await act(async () => {
      rerender(
        <ModalLikeHarness
          appliedValues={{
            name: "Example Login",
            username: "user@example.com",
            password: "",
            totp: "",
            totp_type: "email",
            totp_identifier: "",
          }}
          onChangeSpy={onChangeSpy}
        />,
      );
    });

    const refillCalls = onChangeSpy.mock.calls.filter((call) => {
      const next = call[0] as Values;
      return next.totp_identifier === "user@example.com";
    });
    expect(refillCalls).toEqual([]);
  });

  it("clears totp_identifier when the user switches the method from Email to Text", async () => {
    // Symmetric to the Text → Email reseed case: an email-shaped identifier
    // (or anything from email mode) isn't a valid phone number, so clicking
    // Text on a saved email-TOTP credential must wipe the field.
    const onChangeSpy = vi.fn();

    render(
      <ModalLikeHarness
        appliedValues={{
          name: "Example Login",
          username: "user@example.com",
          password: "",
          totp: "",
          totp_type: "email",
          totp_identifier: "saved-totp-id@example.com",
        }}
        onChangeSpy={onChangeSpy}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Text Message"));
    });

    const clearCall = onChangeSpy.mock.calls.find((call) => {
      const next = call[0] as Values;
      return next.totp_type === "text" && next.totp_identifier === "";
    });
    expect(clearCall).toBeTruthy();
  });

  it("reseeds totp_identifier to username when the user switches the method from Text to Email", async () => {
    // Regression for the Text→Email case raised in PR review: a phone
    // number entered under Text mode must NOT silently persist as the
    // email identifier when the user clicks the Email tile, since it's
    // not a valid email identifier.
    const onChangeSpy = vi.fn();

    render(
      <ModalLikeHarness
        appliedValues={{
          name: "Example Login",
          username: "user@example.com",
          password: "",
          totp: "",
          totp_type: "text",
          totp_identifier: "+14155551234",
        }}
        onChangeSpy={onChangeSpy}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByText("Email"));
    });

    const reseedCall = onChangeSpy.mock.calls.find((call) => {
      const next = call[0] as Values;
      return (
        next.totp_type === "email" &&
        next.totp_identifier === "user@example.com"
      );
    });
    expect(reseedCall).toBeTruthy();
  });

  it("allows QR upload to set the TOTP payload", async () => {
    const onChangeSpy = vi.fn();
    const close = vi.fn();
    const detect = vi
      .fn()
      .mockResolvedValue([{ rawValue: "decoded-qr-payload" }]);
    class BarcodeDetector {
      detect: typeof detect;

      constructor() {
        this.detect = detect;
      }
    }
    vi.stubGlobal("BarcodeDetector", BarcodeDetector);
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({ close }));

    const { container } = render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={{
            name: "Example Login",
            username: "user@example.com",
            password: "password",
            totp: "",
            totp_type: "authenticator",
            totp_identifier: "",
          }}
          onChange={onChangeSpy}
        />
      </MemoryRouter>,
    );
    const input = container.querySelector('input[type="file"]');
    expect(input).toBeTruthy();
    const file = new File(["qr"], "qr.png", { type: "image/png" });

    await act(async () => {
      fireEvent.change(input as HTMLInputElement, {
        target: { files: [file] },
      });
    });

    await waitFor(() =>
      expect(onChangeSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          totp: "decoded-qr-payload",
          totp_type: "authenticator",
        }),
      ),
    );
    expect(close).toHaveBeenCalled();
  });
});

describe("PasswordCredentialContent — supported authenticator copy", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const NEW_VALUES: Values = {
    name: "New cred",
    username: "new-user@example.com",
    password: "password",
    totp: "",
    totp_type: "none",
    totp_identifier: "",
  };

  function stubQrCodeScan(rawValue: string) {
    const imageBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    class MockBarcodeDetector {
      detect = vi.fn().mockResolvedValue([{ rawValue }]);
    }
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(imageBitmap));
    vi.stubGlobal("BarcodeDetector", MockBarcodeDetector);
    return imageBitmap;
  }

  async function uploadQrCodeImage() {
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Upload QR code image"), {
        target: {
          files: [new File(["qr"], "totp.png", { type: "image/png" })],
        },
      });
    });
  }

  async function openAuthenticatorSection(
    enterpriseApps?: typeof ENTERPRISE_APPS,
    onChangeSpy = vi.fn(),
  ) {
    const content = (
      <MemoryRouter>
        <PasswordCredentialContent values={NEW_VALUES} onChange={onChangeSpy} />
      </MemoryRouter>
    );
    render(
      enterpriseApps ? (
        <CredentialAuthenticatorSupportProvider value={{ enterpriseApps }}>
          {content}
        </CredentialAuthenticatorSupportProvider>
      ) : (
        content
      ),
    );
    await act(async () => {
      fireEvent.click(screen.getByText("Two-Factor Authentication"));
    });
    return onChangeSpy;
  }

  it("always lists generic TOTP apps", async () => {
    await openAuthenticatorSection();
    expect(
      screen.getByText(/Google Authenticator, Authy, 1Password/),
    ).toBeTruthy();
  });

  it("omits enterprise QR type options in the default/OSS context", async () => {
    await openAuthenticatorSection();
    expect(
      screen.getByRole("button", { name: /Google Authenticator/ }),
    ).toBeTruthy();
    expect(screen.queryByText("Enterprise Authenticator A")).toBeNull();
    expect(screen.queryByText("Enterprise Authenticator B")).toBeNull();
    expect(
      screen.queryByText(/detects these setup codes automatically/),
    ).toBeNull();
  });

  it("renders Cloud provider QR type options when the context provides apps", async () => {
    await openAuthenticatorSection(ENTERPRISE_APPS);
    const selector = screen.getByTestId("authenticator-qr-type-selector");
    expect(selector).toBeTruthy();
    const keyInput = screen.getByPlaceholderText("e.g. JBSWY3DPEHPK3PXP");
    expect(
      selector.compareDocumentPosition(keyInput) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Google Authenticator/ }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Enterprise Authenticator A" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Enterprise Authenticator B" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/detects these setup codes automatically/),
    ).toBeTruthy();
    expect(
      selector.querySelectorAll('[data-testid="authenticator-type-logo"]'),
    ).toHaveLength(3);
  });

  it("highlights the Cloud provider QR type inferred from an uploaded QR code", async () => {
    const imageBitmap = stubQrCodeScan(
      "example-authenticator://activate?payload=opaque",
    );
    const onChangeSpy = await openAuthenticatorSection(ENTERPRISE_APPS);
    await uploadQrCodeImage();

    await waitFor(() => {
      const qrCall = onChangeSpy.mock.calls.find((call) => {
        const next = call[0] as Values;
        return (
          next.totp_type === "authenticator" &&
          next.totp === "example-authenticator://activate?payload=opaque"
        );
      });
      expect(qrCall).toBeTruthy();
    });
    expect(
      screen
        .getByRole("button", { name: /Enterprise Authenticator A/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen.getByTestId("authenticator-qr-type-detection").textContent,
    ).toBe("Detected from QR");
    expect(imageBitmap.close).toHaveBeenCalled();
  });

  it("announces the inferred type via a polite live region naming the detected app", async () => {
    stubQrCodeScan("example-authenticator://activate?payload=opaque");
    await openAuthenticatorSection(ENTERPRISE_APPS);
    await uploadQrCodeImage();

    const liveRegion = await waitFor(() => {
      const region = screen
        .getByTestId("authenticator-qr-type-detection")
        .closest('[role="status"]');
      expect(region).toBeTruthy();
      return region as HTMLElement;
    });
    expect(liveRegion.getAttribute("aria-live")).toBe("polite");
    expect(liveRegion.textContent).toContain("Detected from QR");
    expect(liveRegion.textContent).toContain("Enterprise Authenticator A");
    expect(
      screen.getByTestId("authenticator-qr-type-detection").textContent,
    ).toBe("Detected from QR");
  });

  it("visually marks only the inferred chip as detected, and clears that marker when the user picks another type", async () => {
    stubQrCodeScan("example-authenticator://activate?payload=opaque");
    await openAuthenticatorSection(ENTERPRISE_APPS);
    await uploadQrCodeImage();

    const inferredButton = await waitFor(() => {
      const button = screen.getByRole("button", {
        name: /Enterprise Authenticator A/,
      });
      expect(button.getAttribute("data-inferred")).toBe("true");
      return button;
    });
    expect(
      screen
        .getByRole("button", { name: /Google Authenticator/ })
        .getAttribute("data-inferred"),
    ).toBeNull();

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /Google Authenticator/ }),
      );
    });

    expect(inferredButton.getAttribute("data-inferred")).toBeNull();
    expect(screen.queryByText("Detected from QR")).toBeNull();
    expect(
      screen
        .getByRole("button", { name: /Google Authenticator/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("does not falsely highlight a Cloud enterprise type for an unknown uploaded QR code", async () => {
    const imageBitmap = stubQrCodeScan(
      "unknown-authenticator://activate?payload=opaque",
    );
    const onChangeSpy = await openAuthenticatorSection(ENTERPRISE_APPS);
    await uploadQrCodeImage();

    await waitFor(() => {
      const qrCall = onChangeSpy.mock.calls.find((call) => {
        const next = call[0] as Values;
        return (
          next.totp_type === "authenticator" &&
          next.totp === "unknown-authenticator://activate?payload=opaque"
        );
      });
      expect(qrCall).toBeTruthy();
    });
    expect(
      screen
        .getByRole("button", { name: /Google Authenticator/ })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(
      screen
        .getByRole("button", { name: "Enterprise Authenticator A" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(
      screen
        .getByRole("button", { name: "Enterprise Authenticator B" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.queryByText("Detected from QR")).toBeNull();
    expect(imageBitmap.close).toHaveBeenCalled();
  });
});

describe("PasswordCredentialContent — inline authenticator save error", () => {
  afterEach(() => {
    cleanup();
  });

  const AUTH_VALUES: Values = {
    name: "New cred",
    username: "new-user@example.com",
    password: "password",
    totp: "otpauth://totp/user@example.com?secret=BAD",
    totp_type: "authenticator",
    totp_identifier: "",
  };

  it("shows enterprise copy with a contact link for an enterprise-required error", () => {
    render(
      <MemoryRouter>
        <CredentialAuthenticatorSupportProvider
          value={{ enterpriseApps: ENTERPRISE_APPS }}
        >
          <PasswordCredentialContent
            values={AUTH_VALUES}
            onChange={vi.fn()}
            authenticatorSaveError={{
              code: "enterprise_required",
              message: "This authenticator requires a Skyvern enterprise plan.",
              vendor: "opaque",
            }}
          />
        </CredentialAuthenticatorSupportProvider>
      </MemoryRouter>,
    );

    const enterpriseMessage = screen.getByText(
      "Enterprise Authenticator B requires a Skyvern enterprise plan.",
    );
    expect(enterpriseMessage.closest(".text-destructive")).toBeNull();
    expect(screen.getByTestId("enterprise-authenticator-upgrade")).toBeTruthy();
    const contactLink = screen.getByRole("link", { name: "Contact us" });
    expect(contactLink.getAttribute("href")).toBe(
      "https://www.skyvern.com/contact",
    );
    const input = screen.getByPlaceholderText("e.g. JBSWY3DPEHPK3PXP");
    expect(input.getAttribute("aria-invalid")).toBe("false");
    expect(input.className).not.toContain("border-destructive");
    expect(input.getAttribute("aria-describedby")).toBeTruthy();
  });

  it("shows actionable no-code-secret copy without a contact link", () => {
    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={AUTH_VALUES}
          onChange={vi.fn()}
          authenticatorSaveError={{
            code: "no_code_secret",
            message:
              "This QR code doesn't contain a code-based setup key. It may enroll a push-approval app or device-bound authenticator. Set up an authenticator app or one-time code instead.",
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/push-approval app/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Contact us" })).toBeNull();
    const input = screen.getByPlaceholderText("e.g. JBSWY3DPEHPK3PXP");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.className).toContain("border-destructive");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toContain(
      "push-approval app",
    );
  });

  it("keeps the decoded QR value in the field after a save failure", () => {
    render(
      <MemoryRouter>
        <PasswordCredentialContent
          values={AUTH_VALUES}
          onChange={vi.fn()}
          authenticatorSaveError={{
            code: "invalid_authenticator_key",
            message: "Invalid authenticator key.",
          }}
        />
      </MemoryRouter>,
    );

    const input = screen.getByPlaceholderText(
      "e.g. JBSWY3DPEHPK3PXP",
    ) as HTMLInputElement;
    expect(input.value).toBe("otpauth://totp/user@example.com?secret=BAD");
  });
});
