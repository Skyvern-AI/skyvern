// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { createContext, useContext, type ReactNode } from "react";
import { describe, expect, test, vi } from "vitest";

import { RunEngine } from "@/api/types";

import { RunEngineSelector } from "./EngineSelector";

vi.mock("./ui/select", () => {
  const SelectValueChangeContext = createContext<(value: string) => void>(
    () => {},
  );

  return {
    Select: ({
      children,
      onValueChange,
    }: {
      children?: ReactNode;
      onValueChange?: (value: string) => void;
    }) => (
      <SelectValueChangeContext.Provider value={onValueChange ?? (() => {})}>
        <div>{children}</div>
      </SelectValueChangeContext.Provider>
    ),
    SelectContent: ({ children }: { children?: ReactNode }) => (
      <div>{children}</div>
    ),
    SelectItem: ({
      children,
      value,
    }: {
      children?: ReactNode;
      value: string;
    }) => {
      const onValueChange = useContext(SelectValueChangeContext);
      return (
        <button type="button" onClick={() => onValueChange(value)}>
          {children}
        </button>
      );
    },
    SelectTrigger: ({ children }: { children?: ReactNode }) => (
      <button type="button">{children}</button>
    ),
    SelectValue: ({ children }: { children?: ReactNode }) => (
      <span>{children}</span>
    ),
  };
});

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

  test("selecting Skyvern 3.0 calls onChange with skyvern-3.0", () => {
    const onChange = vi.fn();
    render(
      <RunEngineSelector
        value={RunEngine.SkyvernV1}
        onChange={onChange}
        availableEngines={[RunEngine.SkyvernV1, RunEngine.SkyvernV3]}
      />,
    );

    fireEvent.click(screen.getByText("Skyvern 3.0"));

    expect(onChange).toHaveBeenCalledWith(RunEngine.SkyvernV3);
  });
});
