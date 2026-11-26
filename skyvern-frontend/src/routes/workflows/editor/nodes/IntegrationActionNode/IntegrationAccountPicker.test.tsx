// @vitest-environment jsdom

import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, test, vi } from "vitest";
import { type ConnectedGmailFlow, PageSlotsProvider } from "@/store/PageSlots";

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
import type { IntegrationCatalogEntryWithActions } from "./types";

describe("integration account picker", () => {
  test("uses server fit and limits injected accounts to supported actions", () => {
    const native = render(
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
    native.unmount();

    const flow: ConnectedGmailFlow = {
      status: "default",
      canConnect: true,
      busy: false,
      failure: null,
      connections: [
        {
          connection_id: "ngc_drive",
          name: "Team Drive",
          email_address: "o***r@example.test",
        },
      ],
      start: () => {},
    };
    const slots = {
      connectedGoogleDriveFlow: ({
        children,
      }: {
        children: (value: ConnectedGmailFlow) => ReactNode;
      }) => <>{children(flow)}</>,
    };
    const entry: IntegrationCatalogEntryWithActions = {
      id: "google_drive",
      name: "Google Drive",
      status: "live" as const,
      oauthProvider: "google" as const,
      actions: [
        {
          id: "list_folder",
          name: "List folder",
          description: "List folder",
          inputs: {},
          acts: false,
          credentialBackends: ["native", "nango"],
          runsVia: "runner:google_drive_folder",
        },
        {
          id: "create_folder",
          name: "Create folder",
          description: "Create folder",
          inputs: {},
          acts: true,
          credentialBackends: ["native"],
          runsVia: "runner:google_drive_folder",
        },
      ],
    };
    const read = render(
      <PageSlotsProvider value={slots}>
        <IntegrationAccountPicker
          nodeId="n1"
          entry={entry}
          actionKey="google_drive.list_folder"
          value="ngc_drive"
          onChange={() => {}}
        />
      </PageSlotsProvider>,
    );
    expect(
      screen.queryByText(
        "Saved account is no longer connected. Pick another below.",
      ),
    ).toBeNull();
    fireEvent.click(
      screen.getByRole("combobox", { name: "Google Drive account" }),
    );
    const connected = screen.getByRole("option", {
      name: "Team Drive (o***r@example.test)",
    });
    expect(within(connected).getByText("Connected account")).toBeTruthy();
    read.unmount();

    render(
      <PageSlotsProvider value={slots}>
        <IntegrationAccountPicker
          nodeId="n1"
          entry={entry}
          actionKey="google_drive.create_folder"
          value="ngc_drive"
          onChange={() => {}}
        />
      </PageSlotsProvider>,
    );
    expect(
      screen.getByText(
        "Connected Drive accounts do not support Create folder. Pick a native account below.",
      ),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("combobox", { name: "Google Drive account" }),
    );
    expect(
      screen.queryByRole("option", {
        name: "Team Drive (o***r@example.test)",
      }),
    ).toBeNull();
  });
});
