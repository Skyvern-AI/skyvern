// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InteractiveStreamView } from "./InteractiveStreamView";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;

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
      handlePaste: vi.fn(),
    },
    currentUrl: "https://example.com/",
  };
}

describe("InteractiveStreamView URL bar", () => {
  it("stays a read-only display when no onNavigate is passed, with no disabled cue", () => {
    render(<InteractiveStreamView {...baseProps()} />);

    expect(screen.getByText("https://example.com/")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("https://example.com/").closest("div")!.title).toBe(
      "",
    );
  });

  it("stays read-only and visibly disabled when onNavigate is passed but the user has not taken control", () => {
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={false}
        onNavigate={vi.fn()}
      />,
    );

    expect(screen.getByText("https://example.com/")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    const bar = screen.getByText("https://example.com/").closest("div")!;
    expect(bar.className).toContain("cursor-not-allowed");
    expect(bar.title).toBe("Take control to edit the URL");
  });

  it("uses the same horizontal padding as the editable bar so the icon doesn't jump on take/release control", () => {
    const { rerender } = render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={false}
        onNavigate={vi.fn()}
      />,
    );
    const readOnlyBar = screen
      .getByText("https://example.com/")
      .closest("div")!;
    const readOnlyPadding = readOnlyBar.className.match(/\bpx-\d+\b/)?.[0];

    rerender(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={vi.fn()}
      />,
    );
    const navigableBar = screen.getByRole("textbox").closest("form")!;
    const navigablePadding = navigableBar.className.match(/\bpx-\d+\b/)?.[0];

    expect(readOnlyPadding).toBeTruthy();
    expect(readOnlyPadding).toBe(navigablePadding);
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

  it("does not forward paste events from the URL input to the remote page", () => {
    const handlePaste = vi.fn();
    const props = baseProps();
    render(
      <InteractiveStreamView
        {...props}
        userIsControlling={true}
        onNavigate={vi.fn()}
        handlers={{ ...props.handlers, handlePaste }}
      />,
    );

    fireEvent.paste(screen.getByRole("textbox"), {
      clipboardData: { getData: () => "https://iana.org" },
    });

    expect(handlePaste).not.toHaveBeenCalled();
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

describe("InteractiveStreamView input forwarding", () => {
  it("forwards paste events from the focused stream container", () => {
    const handlePaste = vi.fn();
    const props = baseProps();
    const { container } = render(
      <InteractiveStreamView
        {...props}
        handlers={{ ...props.handlers, handlePaste }}
      />,
    );

    const streamContainer = container.querySelector('[tabindex="0"]')!;
    fireEvent.paste(streamContainer, {
      clipboardData: { getData: () => "from clipboard" },
    });

    expect(handlePaste).toHaveBeenCalledTimes(1);
  });
});

describe("InteractiveStreamView browser chrome", () => {
  it("renders no nav controls for callers that don't wire history navigation", () => {
    render(<InteractiveStreamView {...baseProps()} onNavigate={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
  });

  it("disables the nav controls until the user takes control, matching the URL bar", () => {
    const onHistoryNavigate = vi.fn();
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={false}
        onNavigate={vi.fn()}
        onHistoryNavigate={onHistoryNavigate}
      />,
    );

    const back = screen.getByRole("button", {
      name: "Back",
    }) as HTMLButtonElement;
    expect(back.disabled).toBe(true);
    fireEvent.click(back);
    expect(onHistoryNavigate).not.toHaveBeenCalled();
  });

  it("sends the matching history action once the user is controlling", () => {
    const onHistoryNavigate = vi.fn();
    render(
      <InteractiveStreamView
        {...baseProps()}
        userIsControlling={true}
        onNavigate={vi.fn()}
        onHistoryNavigate={onHistoryNavigate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    expect(onHistoryNavigate.mock.calls.map((c) => c[0])).toEqual([
      "back",
      "forward",
      "reload",
    ]);
  });

  it("spins the reload icon on click so the reload is visible, then settles", () => {
    vi.useFakeTimers();
    try {
      render(
        <InteractiveStreamView
          {...baseProps()}
          userIsControlling={true}
          onNavigate={vi.fn()}
          onHistoryNavigate={vi.fn()}
        />,
      );

      const reload = screen.getByRole("button", { name: "Reload" });
      expect(reload.querySelector(".animate-spin")).toBeNull();

      fireEvent.click(reload);
      expect(reload.querySelector(".animate-spin")).not.toBeNull();

      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(reload.querySelector(".animate-spin")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

// Swaps in a ResizeObserver whose callback the test can fire by hand, then reports
// one resize of `height` for an image with the given intrinsic size. Restores the
// original global before returning.
function observeOneResize({
  height,
  naturalWidth,
  naturalHeight,
}: {
  height: number;
  naturalWidth: number;
  naturalHeight: number;
}) {
  let capturedCallback: ResizeObserverCallback | undefined;
  class CapturingResizeObserver {
    constructor(cb: ResizeObserverCallback) {
      capturedCallback = cb;
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  const original = (globalThis as { ResizeObserver: unknown }).ResizeObserver;
  (globalThis as { ResizeObserver: unknown }).ResizeObserver =
    CapturingResizeObserver;

  return {
    fire(render: () => void) {
      render();
      const img = document.querySelector("img") as HTMLImageElement;
      Object.defineProperty(img, "naturalWidth", {
        value: naturalWidth,
        configurable: true,
      });
      Object.defineProperty(img, "naturalHeight", {
        value: naturalHeight,
        configurable: true,
      });
      act(() => {
        capturedCallback?.(
          [{ contentRect: { height } } as ResizeObserverEntry],
          {} as ResizeObserver,
        );
      });
      return img;
    },
    restore() {
      (globalThis as { ResizeObserver: unknown }).ResizeObserver = original;
    },
  };
}

describe("InteractiveStreamView preview width", () => {
  it("derives width from observed height and the image's aspect ratio, not the fed-back width", () => {
    const observer = observeOneResize({
      height: 450,
      naturalWidth: 1600,
      naturalHeight: 900,
    });

    const img = observer.fire(() =>
      render(<InteractiveStreamView {...baseProps()} />),
    );

    expect(img.parentElement?.style.width).toBe("800px");

    observer.restore();
  });

  it("hands the measured width to a parent that owns the frame instead of framing itself", () => {
    const onFrameWidthChange = vi.fn();
    const observer = observeOneResize({
      height: 450,
      naturalWidth: 1600,
      naturalHeight: 900,
    });

    let unmount = () => {};
    const img = observer.fire(() => {
      ({ unmount } = render(
        <InteractiveStreamView
          {...baseProps()}
          onFrameWidthChange={onFrameWidthChange}
        />,
      ));
    });

    expect(onFrameWidthChange).toHaveBeenCalledWith(800);
    // The parent draws the window at that width, so we must not also draw one.
    expect(img.parentElement?.style.width).toBe("");
    expect(img.parentElement?.className).not.toContain("shadow-elevated");

    // Otherwise the parent's frame stays pinned to a preview that's gone.
    unmount();
    expect(onFrameWidthChange).toHaveBeenLastCalledWith(null);

    observer.restore();
  });
});
