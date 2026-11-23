// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      aria-label="connection id"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));
vi.mock("@/hooks/useGoogleOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useGoogleOAuthCredentials: () => ({
    credentials: [
      {
        id: "goc_1",
        state: "active",
        credential_name: "Work",
        action_fit: { "gmail.send_email": "no" },
      },
    ],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useMicrosoftOAuthCredentials: () => ({
    credentials: [],
    isLoading: false,
    error: null,
  }),
}));

import { IntegrationAccountPicker } from "./IntegrationAccountPicker";

describe("integration account picker", () => {
  test("the server's no for this action flags the saved account with a reconnect hint", () => {
    render(
      <IntegrationAccountPicker
        nodeId="n1"
        entry={{
          id: "gmail",
          name: "Gmail",
          status: "live",
          oauthProvider: "google",
        }}
        actionKey="gmail.send_email"
        value="goc_1"
        onChange={() => {}}
      />,
    );
    expect(
      screen.getByText(
        "Missing permission for this action. Reconnect it on Integrations.",
      ),
    ).toBeTruthy();
  });
});
