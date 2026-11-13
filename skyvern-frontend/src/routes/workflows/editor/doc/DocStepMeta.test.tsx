// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { useContext } from "react";

import type { WorkflowBlockNode } from "../nodes";
import { loginNodeDefaultData } from "../nodes/LoginNode/types";
import { docKeySettings, docSettingRowsSplit } from "./docSettingsChips";
import { DocStepMeta } from "./DocStepMeta";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

afterEach(cleanup);

const login = (data: Record<string, unknown>) =>
  ({
    id: "n1",
    position: { x: 0, y: 0 },
    type: "login",
    data: { ...loginNodeDefaultData, ...data, label: "sample_login" },
  }) as unknown as WorkflowBlockNode;

function renderMeta(node: WorkflowBlockNode, editable: boolean) {
  const openSettings = vi.fn();
  const rows = docSettingRowsSplit(
    docKeySettings(node, new Set(["sample_credential"])),
    editable,
    false,
  ).line;
  function Meta() {
    const actions = useContext(WorkflowDocActionsContext);
    return (
      <WorkflowDocActionsContext.Provider
        value={{
          ...actions,
          openSettings,
          stepLinesEditable: () => editable,
        }}
      >
        <DocStepMeta node={node} rows={rows} />
      </WorkflowDocActionsContext.Provider>
    );
  }
  render(<Meta />);
  return openSettings;
}

describe("Doc step summary line", () => {
  test("a new login's dashed chips open the step's settings", () => {
    const openSettings = renderMeta(login({}), true);
    fireEvent.click(
      screen.getByRole("button", { name: "Add Credential to Sample login" }),
    );
    expect(openSettings).toHaveBeenCalledWith("n1");
    const url = screen.getByRole("button", {
      name: "Add Login page URL to Sample login",
    });
    // A Login can run without either, so neither chip is marked required.
    expect(url.querySelector("[data-doc-meta-required]")).toBeNull();
  });

  test("a filled login reads credential and host, never a user:password part; its values open the settings too", () => {
    const openSettings = renderMeta(
      login({
        parameterKeys: ["sample_credential"],
        url: "https://user:pw@example.com/sign-in?next=/home",
      }),
      true,
    );
    expect(document.body.innerHTML).not.toContain("user:pw");
    expect(screen.getByText("example.com")).toBeTruthy();
    expect(screen.queryByText(/sign-in/)).toBeNull();
    const url = screen.getByRole("button", { name: /^URL: / });
    expect(url.getAttribute("title")).toBe(
      "https://example.com/sign-in?next=/home",
    );
    fireEvent.click(url);
    expect(openSettings).toHaveBeenCalledWith("n1");
    expect(screen.getByText("sample_credential")).toBeTruthy();
  });

  test.each([
    "https://{{ user }}:hunter2@example.com/p",
    "//user:hunter2@example.com/p",
  ])(
    "%s: the password is in neither the text, the tooltip nor the label",
    (url) => {
      renderMeta(login({ parameterKeys: ["sample_credential"], url }), true);
      const line = screen.getByRole("button", { name: /^URL: / });
      expect(line.getAttribute("aria-label")).not.toContain("hunter2");
      expect(line.getAttribute("title")).not.toContain("hunter2");
      expect(document.body.innerHTML).not.toContain("hunter2");
    },
  );

  test("read-only: no chips and no buttons, only the set values", () => {
    renderMeta(login({ url: "https://example.com/a" }), false);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("example.com")).toBeTruthy();
    expect(screen.queryByText("Credential")).toBeNull();
  });
});
