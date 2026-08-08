// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InteractiveStreamView } from "./InteractiveStreamView";

afterEach(cleanup);

function baseProps() {
  return {
    streamImgSrc: "abc123",
    streamFormat: "png",
    interactive: true,
    userIsControlling: false,
    setUserIsControlling: vi.fn(),
    inputReady: true,
    containerRef: { current: null },
    showControlButtons: true,
    handlers: {
      handleMouseDown: vi.fn(),
      handleMouseUp: vi.fn(),
      handleMouseMove: vi.fn(),
      handleKeyDown: vi.fn(),
      handleKeyUp: vi.fn(),
    },
    currentUrl: "https://example.com/",
  };
}

describe("InteractiveStreamView URL bar", () => {
  it("stays a read-only display when no onNavigate is passed", () => {
    render(<InteractiveStreamView {...baseProps()} />);

    expect(screen.getByText("https://example.com/")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("stays read-only when onNavigate is passed but the user has not taken control", () => {
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={false}
        onNavigate={vi.fn()}
      />,
    );

    expect(screen.getByText("https://example.com/")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("becomes an editable input once the user has taken control, and submits on Enter", () => {
    const onNavigate = vi.fn();
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
      />,
    );

    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("https://example.com/");

    fireEvent.change(input, { target: { value: "https://iana.org  " } });
    fireEvent.submit(input.closest("form")!);

    expect(onNavigate).toHaveBeenCalledWith("https://iana.org");
  });

  it("does not forward keystrokes typed into the URL input to the remote page", () => {
    const handleKeyDown = vi.fn();
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={vi.fn()}
        handlers={{
          ...baseProps().handlers,
          handleKeyDown,
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "a" });

    expect(handleKeyDown).not.toHaveBeenCalled();
  });

  it("shows a navigate error inline without breaking the input", () => {
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={vi.fn()}
        navigateError="That destination isn't allowed."
      />,
    );

    expect(screen.getByText("That destination isn't allowed.")).toBeTruthy();
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("does not overwrite a focused, mid-typed value when the remote URL changes", () => {
    const onNavigate = vi.fn();
    const { rerender } = render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
      />,
    );

    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "https://draft.example" } });

    rerender(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
        currentUrl="https://elsewhere.example/"
      />,
    );

    expect(input.value).toBe("https://draft.example");
  });

  it("syncs to the remote URL once the input loses focus", () => {
    const onNavigate = vi.fn();
    const { rerender } = render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
      />,
    );

    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "https://draft.example" } });
    fireEvent.blur(input);

    rerender(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
        currentUrl="https://elsewhere.example/"
      />,
    );

    expect(input.value).toBe("https://elsewhere.example/");
  });

  it("ignores an empty submission", () => {
    const onNavigate = vi.fn();
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={onNavigate}
      />,
    );

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.submit(input.closest("form")!);

    expect(onNavigate).not.toHaveBeenCalled();
  });
});
