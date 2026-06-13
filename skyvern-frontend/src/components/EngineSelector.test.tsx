// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, test, vi } from "vitest";

import { RunEngine } from "@/api/types";

import { RunEngineSelector } from "./EngineSelector";

vi.mock("./ui/select", () => ({
  Select: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  SelectItem: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  SelectTrigger: ({ children }: { children?: ReactNode }) => (
    <button type="button">{children}</button>
  ),
  SelectValue: ({ children }: { children?: ReactNode }) => (
    <span>{children}</span>
  ),
}));

describe("RunEngineSelector", () => {
  test("hides Yutori Navigator by default", () => {
    render(
      <RunEngineSelector value={RunEngine.SkyvernV1} onChange={() => {}} />,
    );

    expect(screen.queryByText("Yutori Navigator")).toBeNull();
  });

  test("keeps selected Yutori Navigator visible as deprecated", () => {
    render(
      <RunEngineSelector
        value={RunEngine.YutoriNavigator}
        onChange={() => {}}
      />,
    );

    expect(screen.getAllByText("Yutori Navigator").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Deprecated").length).toBeGreaterThan(0);
  });

  test.each([
    [RunEngine.OpenaiCua, "OpenAI CUA"],
    [RunEngine.AnthropicCua, "Anthropic CUA"],
  ])("marks %s as enterprise-only", (engine, label) => {
    render(
      <RunEngineSelector
        value={engine}
        onChange={() => {}}
        availableEngines={[engine]}
      />,
    );

    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Enterprise").length).toBeGreaterThan(0);
  });
});
