// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CloudContext from "@/store/CloudContext";

const credentialsQuery = vi.hoisted(() => ({ isSuccess: false }));

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => credentialsQuery,
}));
vi.mock("@/hooks/useCustomCredentialServiceConfig", () => ({
  useCustomCredentialServiceConfig: () => ({ parsedConfig: null }),
}));
vi.mock("../../hooks/useOnePasswordItemsQuery", () => ({
  useOnePasswordItemsQuery: () => ({ data: undefined, isError: false }),
}));
vi.mock("../../components/CredentialParameterSourceSelector", () => ({
  CredentialParameterSourceSelector: () => <div>Vault credential list</div>,
}));
vi.mock("../../components/BitwardenItemSelector", () => ({
  BitwardenItemSelector: () => <div>Bitwarden item list</div>,
}));

import { WorkflowParameterEditPanel } from "./WorkflowParameterEditPanel";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  credentialsQuery.isSuccess = false;
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  window.HTMLElement.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function panel(isCloud: boolean) {
  return (
    <CloudContext.Provider value={isCloud}>
      <WorkflowParameterEditPanel
        type="workflow"
        onClose={vi.fn()}
        onSave={vi.fn()}
      />
    </CloudContext.Provider>
  );
}

function enterCredentialMode() {
  const valueTypeSelect = screen.getAllByRole("combobox")[0];
  if (!valueTypeSelect) throw new Error("Value Type select not found");
  fireEvent.click(valueTypeSelect);
  fireEvent.click(screen.getByRole("option", { name: "credential" }));
}

function openSourceSelect() {
  const sourceSelect = screen.getAllByRole("combobox")[2];
  if (!sourceSelect) throw new Error("Source select not found");
  fireEvent.click(sourceSelect);
}

function renderPanel(isCloud: boolean) {
  render(panel(isCloud));

  enterCredentialMode();
  openSourceSelect();
}

describe("WorkflowParameterEditPanel Skyvern source availability", () => {
  it("shows Skyvern in OSS when the credentials query succeeds", () => {
    credentialsQuery.isSuccess = true;
    renderPanel(false);

    expect(screen.getByRole("option", { name: "Skyvern" })).toBeTruthy();
  });

  it("hides Skyvern in OSS when the credentials query errors", () => {
    credentialsQuery.isSuccess = false;
    renderPanel(false);

    expect(screen.queryByRole("option", { name: "Skyvern" })).toBeNull();
  });

  it("shows Skyvern in cloud regardless of the credentials query", () => {
    credentialsQuery.isSuccess = false;
    renderPanel(true);

    expect(screen.getByRole("option", { name: "Skyvern" })).toBeTruthy();
  });

  it("updates the untouched OSS default when the capability probe succeeds", async () => {
    const view = render(panel(false));
    enterCredentialMode();
    expect(screen.getAllByRole("combobox")[2]?.textContent).toContain(
      "Bitwarden",
    );

    credentialsQuery.isSuccess = true;
    view.rerender(panel(false));

    await waitFor(() =>
      expect(screen.getAllByRole("combobox")[2]?.textContent).toContain(
        "Skyvern",
      ),
    );
  });
});
