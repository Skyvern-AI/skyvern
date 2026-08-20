// @vitest-environment jsdom

import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import { OtpType } from "@/api/types";
import { PushTotpCodeForm } from "./PushTotpCodeForm";
import { buildSendTotpCodeRequest } from "./pushTotpCodeRequest";

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast }),
}));

const mockedGetClient = vi.mocked(getClient);

function renderForm(fixedOtpType?: (typeof OtpType)[keyof typeof OtpType]) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });

  return render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(PushTotpCodeForm, { fixedOtpType }),
    ),
  );
}

function selectOtpType(container: HTMLElement, otpType: string) {
  const otpTypeSelect = container.querySelector("select");
  expect(otpTypeSelect).not.toBeNull();
  fireEvent.change(otpTypeSelect!, { target: { value: otpType } });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("buildSendTotpCodeRequest", () => {
  it("includes explicit magic-link type and optional metadata", () => {
    expect(
      buildSendTotpCodeRequest({
        identifier: " user@example.com ",
        content: " https://example.com/login?token=abc ",
        otpType: OtpType.MagicLink,
        workflowRunId: " wr_123 ",
        workflowId: " wf_123 ",
        taskId: " tsk_123 ",
      }),
    ).toEqual({
      totp_identifier: "user@example.com",
      content: "https://example.com/login?token=abc",
      type: OtpType.MagicLink,
      source: "manual_ui",
      workflow_run_id: "wr_123",
      workflow_id: "wf_123",
      task_id: "tsk_123",
    });
  });

  it("omits blank optional metadata for numeric codes", () => {
    expect(
      buildSendTotpCodeRequest({
        identifier: " user@example.com ",
        content: " 123456 ",
        otpType: OtpType.Totp,
        workflowRunId: " ",
        workflowId: "",
        taskId: "",
      }),
    ).toEqual({
      totp_identifier: "user@example.com",
      content: "123456",
      type: OtpType.Totp,
      source: "manual_ui",
    });
  });
});

describe("PushTotpCodeForm", () => {
  it("uses magic-link wording for the identifier placeholder", () => {
    renderForm(OtpType.MagicLink);

    expect(
      screen.getByPlaceholderText("Email receiving the magic link"),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "OTP Type" })).toBeNull();
  });

  it("blocks fixed magic-link submissions without an http(s) link", () => {
    const post = vi.fn().mockResolvedValue({});
    mockedGetClient.mockResolvedValue({ post } as never);
    renderForm(OtpType.MagicLink);

    fireEvent.change(
      screen.getByPlaceholderText("Email receiving the magic link"),
      {
        target: { value: "user@example.com" },
      },
    );
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: "Your verification code is 123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Magic Link" }));

    expect(
      screen.getByText(
        "Paste the full magic link message — no http(s) link found.",
      ),
    ).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it("allows non-fixed magic-link submissions for backend classification", async () => {
    const post = vi.fn().mockResolvedValue({});
    mockedGetClient.mockResolvedValue({ post } as never);
    const { container } = renderForm();

    selectOtpType(container, OtpType.MagicLink);

    expect(
      screen.getByPlaceholderText("Email receiving the magic link"),
    ).toBeTruthy();
    expect(
      screen.getByPlaceholderText("Paste the full email body or magic link"),
    ).toBeTruthy();

    fireEvent.change(
      screen.getByPlaceholderText("Email receiving the magic link"),
      {
        target: { value: "user@example.com" },
      },
    );
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: "Your verification code is 123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Magic Link" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/credentials/totp", {
        totp_identifier: "user@example.com",
        content: "Your verification code is 123456",
        type: OtpType.MagicLink,
        source: "manual_ui",
      });
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("uses magic-link toast copy when the selected type is submitted", async () => {
    const post = vi.fn().mockResolvedValue({});
    mockedGetClient.mockResolvedValue({ post } as never);
    const { container } = renderForm();

    selectOtpType(container, OtpType.MagicLink);
    fireEvent.change(screen.getByLabelText("Identifier"), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: "Open https://example.com/login?token=abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Magic Link" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/credentials/totp", {
        totp_identifier: "user@example.com",
        content: "Open https://example.com/login?token=abc",
        type: OtpType.MagicLink,
        source: "manual_ui",
      });
      expect(toast).toHaveBeenCalledWith({
        title: "Magic link sent",
        description: "Skyvern will process it shortly.",
      });
    });
  });

  it("uses magic-link error copy when the selected type fails", async () => {
    const post = vi.fn().mockRejectedValue(new Error("request failed"));
    mockedGetClient.mockResolvedValue({ post } as never);
    const { container } = renderForm();

    selectOtpType(container, OtpType.MagicLink);
    fireEvent.change(screen.getByLabelText("Identifier"), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: "https://example.com/login?token=abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Magic Link" }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith({
        variant: "destructive",
        title: "Failed to send magic link",
        description: "Check the identifier and message format, then retry.",
      });
    });
  });

  it("keeps default numeric-code submissions unchanged", async () => {
    const post = vi.fn().mockResolvedValue({});
    mockedGetClient.mockResolvedValue({ post } as never);
    renderForm();

    fireEvent.change(
      screen.getByPlaceholderText("Email or phone receiving the code"),
      {
        target: { value: " user@example.com " },
      },
    );
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: " 123456 " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send 2FA Code" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/credentials/totp", {
        totp_identifier: "user@example.com",
        content: "123456",
        type: OtpType.Totp,
        source: "manual_ui",
      });
    });
  });
});
