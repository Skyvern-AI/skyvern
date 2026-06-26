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
    expect(imageBitmap.close).toHaveBeenCalled();
  });

  it("rejects QR codes that are not 2FA setup values", async () => {
    const onChangeSpy = vi.fn();
    const imageBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    class MockBarcodeDetector {
      detect = vi.fn().mockResolvedValue([
        {
          rawValue: "https://example.com/account/settings",
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
      expect(
        screen.getByText(
          "This QR code doesn't look like a 2FA setup code. Make sure you're scanning the setup QR from the site's 2FA settings.",
        ),
      ).toBeTruthy();
    });
    expect(imageBitmap.close).toHaveBeenCalled();
    expect(
      onChangeSpy.mock.calls.some((call) => {
        const next = call[0] as Values;
        return next.totp === "https://example.com/account/settings";
      }),
    ).toBe(false);
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
});
