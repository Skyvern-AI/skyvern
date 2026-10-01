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
    render(<RunEngineSelector value={null} onChange={() => {}} />);

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

  test("selecting Default or Skyvern 3.0 calls onChange with null or skyvern-3.0", () => {
    const onChange = vi.fn();
    render(
      <RunEngineSelector
        value={RunEngine.SkyvernV2}
        onChange={onChange}
        availableEngines={[RunEngine.SkyvernV1, RunEngine.SkyvernV3]}
      />,
    );

    fireEvent.click(screen.getByText("Skyvern 3.0"));
    fireEvent.click(screen.getByText("Default"));

    expect(onChange.mock.calls).toEqual([[RunEngine.SkyvernV3], [null]]);
  });

  test.each([null, RunEngine.SkyvernV3])(
    "labels Default with no hint when the effective default is %s",
    (effectiveDefaultEngine) => {
      const { container } = render(
        <RunEngineSelector
          value={null}
          onChange={() => {}}
          effectiveDefaultEngine={effectiveDefaultEngine}
        />,
      );

      const defaultItem = screen
        .getAllByRole("button")
        .filter((button) => button.textContent?.includes("Default"));
      expect(defaultItem.map((button) => button.textContent)).toEqual([
        "Default",
        "Default",
      ]);
      expect(container.textContent).not.toMatch(/routing|runs on/i);
    },
  );

  test("offers Skyvern 1.0 as Legacy only where the workflow can pin it", () => {
    const { unmount } = render(
      <RunEngineSelector value={null} onChange={() => {}} />,
    );
    expect(screen.queryByText("Skyvern 1.0")).toBeNull();
    unmount();

    render(
      <RunEngineSelector
        value={null}
        onChange={() => {}}
        effectiveDefaultEngine={RunEngine.SkyvernV3}
      />,
    );
    expect(screen.getByText("Skyvern 1.0")).toBeTruthy();
    expect(screen.getByText("Legacy")).toBeTruthy();
  });

  test("marks Skyvern 3.0 as Recommended", () => {
    render(
      <RunEngineSelector
        value={RunEngine.SkyvernV3}
        onChange={() => {}}
        availableEngines={[RunEngine.SkyvernV1, RunEngine.SkyvernV3]}
      />,
    );

    expect(screen.getAllByText("Skyvern 3.0").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Recommended").length).toBeGreaterThan(0);
  });

  test("marks Skyvern 2.0 as Legacy", () => {
    render(
      <RunEngineSelector
        value={RunEngine.SkyvernV2}
        onChange={() => {}}
        availableEngines={[RunEngine.SkyvernV1, RunEngine.SkyvernV2]}
      />,
    );

    expect(screen.getAllByText("Skyvern 2.0").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Legacy").length).toBeGreaterThan(0);
  });
});
