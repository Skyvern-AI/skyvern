// @vitest-environment jsdom

import {
  fireEvent,
  render as renderInDom,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";

const catalogState = vi.hoisted(() => ({
  data: undefined as unknown[] | undefined,
}));

vi.mock("@/hooks/useIntegrationCatalogQuery", () => ({
  useIntegrationCatalogQuery: () => ({ data: catalogState.data }),
}));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("@/store/PageSlots", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  usePageSlots: () => ({ workflowTriggersPanel: () => null }),
}));
vi.mock("@/hooks/useGoogleOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useGoogleOAuthCredentials: () => ({
    isSuccess: true,
    credentials: [
      { id: "goc_1", state: "active", scopes_granted: ["gmail.readonly"] },
    ],
  }),
}));
vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useMicrosoftOAuthCredentials: () => ({ isSuccess: true, credentials: [] }),
}));

import {
  StudioPaneDefaultsContext,
  useStudioPaneDefaults,
} from "../../studio/StudioPaneDefaultsContext";
import type { StudioPaneId } from "../../studio/panes";
import { WorkflowNodeLibraryPanel } from "./WorkflowNodeLibraryPanel";

let openPanes: StudioPaneId[] = [];
function Studio({ children }: { children: ReactNode }) {
  const defaults = useStudioPaneDefaults();
  return (
    <MemoryRouter>
      <StudioPaneDefaultsContext.Provider
        value={{
          ...defaults,
          updatePanes: (compute) => {
            const next = compute(openPanes, openPanes, 0);
            openPanes = Array.isArray(next) ? next : next.panes;
          },
        }}
      >
        {children}
      </StudioPaneDefaultsContext.Provider>
    </MemoryRouter>
  );
}
const render = (ui: React.ReactElement) => renderInDom(ui, { wrapper: Studio });

describe("WorkflowNodeLibraryPanel Power BI availability", () => {
  beforeEach(() => {
    catalogState.data = undefined;
  });

  test("fails closed while the catalog is unavailable or Power BI is coming soon", () => {
    const onNodeClick = vi.fn();
    const { rerender } = render(
      <WorkflowNodeLibraryPanel onNodeClick={onNodeClick} />,
    );
    expect(screen.queryByTestId("block-library-item-powerBIQuery")).toBeNull();

    catalogState.data = [{ id: "powerbi", status: "comingSoon" }];
    rerender(<WorkflowNodeLibraryPanel onNodeClick={onNodeClick} />);
    expect(screen.queryByTestId("block-library-item-powerBIQuery")).toBeNull();
    expect(onNodeClick).not.toHaveBeenCalled();
  });

  test("shows Power BI only when the served catalog marks it live", () => {
    catalogState.data = [{ id: "powerbi", status: "live" }];
    render(<WorkflowNodeLibraryPanel onNodeClick={vi.fn()} />);

    expect(screen.getByTestId("block-library-item-powerBIQuery")).toBeTruthy();
  });
});

describe("WorkflowNodeLibraryPanel apps", () => {
  const sendEmail = {
    id: "send_email",
    name: "Send email",
    description: "",
    runsVia: "block:send_email",
    inputs: { properties: { transport: { const: "gmail" } } },
  };
  const listLabels = {
    id: "list_labels",
    name: "List labels",
    description: "",
    inputs: {},
  };
  beforeEach(() => {
    catalogState.data = [
      {
        id: "outlook",
        name: "Outlook",
        status: "live",
        oauthProvider: "microsoft",
        actions: [sendEmail],
      },
      {
        id: "gmail",
        name: "Gmail",
        status: "live",
        oauthProvider: "google",
        requiredScopes: ["gmail.readonly"],
        actions: [sendEmail, listLabels],
        triggers: [{ id: "new_email", name: "New email" }],
      },
    ];
  });

  test("lists connected apps first and adds the block an app action runs", () => {
    const added: Array<Record<string, unknown>> = [];
    render(
      <WorkflowNodeLibraryPanel onNodeClick={(props) => added.push(props)} />,
    );
    const apps = screen.getAllByTestId(/^block-library-app-[a-z]+$/);
    expect(apps.map((row) => row.dataset.testid)).toEqual([
      "block-library-app-gmail",
      "block-library-app-outlook",
    ]);
    expect(apps[0]?.textContent).toContain("Account connected");
    expect(apps[1]?.textContent).toContain("No account connected");

    fireEvent.click(screen.getByTestId("block-library-app-gmail"));
    expect(added).toEqual([]);
    fireEvent.click(
      screen.getByTestId("block-library-app-trigger-gmail-new_email"),
    );
    expect(openPanes).toContain("triggers");

    fireEvent.click(
      screen.getByTestId("block-library-app-action-gmail-send_email"),
    );
    fireEvent.click(
      screen.getByTestId("block-library-app-action-gmail-list_labels"),
    );
    expect(added).toMatchObject([
      { nodeType: "sendEmail", data: { transport: "gmail" } },
      {
        nodeType: "integrationAction",
        label: "gmail",
        integrationAction: { integrationId: "gmail", actionId: "list_labels" },
      },
    ]);
  });

  test("search finds the action in every app next to the built-in block", () => {
    render(<WorkflowNodeLibraryPanel onNodeClick={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("Search blocks..."), {
      target: { value: "send email" },
    });
    expect(
      screen.getByTestId("block-library-app-action-gmail-send_email"),
    ).toBeTruthy();
    expect(
      screen.getByTestId("block-library-app-action-outlook-send_email"),
    ).toBeTruthy();
    expect(screen.getByTestId("block-library-item-sendEmail")).toBeTruthy();
  });
});
