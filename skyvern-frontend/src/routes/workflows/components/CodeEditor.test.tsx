// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

type CmMockProps = {
  onCreateEditor?: unknown;
  onUpdate?: unknown;
  onChange?: unknown;
  onBlur?: unknown;
  value?: string;
  theme?: unknown;
};

const cmMockCalls: CmMockProps[] = [];

const themeModeMock = vi.hoisted(() => vi.fn());
vi.mock("@/components/useThemeAsDarkOrLight", () => ({
  useThemeAsDarkOrLight: () => themeModeMock(),
}));

vi.mock("@uiw/react-codemirror", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@uiw/react-codemirror")>();
  return {
    ...actual,
    default: (props: CmMockProps) => {
      cmMockCalls.push(props);
      return <div className="cm-editor" data-testid="cm-mock" />;
    },
  };
});

beforeEach(() => {
  cmMockCalls.length = 0;
  themeModeMock.mockReturnValue("dark");
});

import { CodeEditor } from "./CodeEditor";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { tokyoNightDay } from "@uiw/codemirror-theme-tokyo-night-day";

// Drive the IntersectionObserver mock so we can flip visibility deterministically.
let observerCallback: IntersectionObserverCallback | null = null;
const observeMock = vi.fn();
const disconnectMock = vi.fn();

beforeEach(() => {
  observerCallback = null;
  observeMock.mockClear();
  disconnectMock.mockClear();
  class FakeIntersectionObserver {
    constructor(cb: IntersectionObserverCallback) {
      observerCallback = cb;
    }
    observe = observeMock;
    disconnect = disconnectMock;
    unobserve = vi.fn();
    takeRecords = () => [];
    root = null;
    rootMargin = "";
    thresholds = [];
  }
  globalThis.IntersectionObserver =
    FakeIntersectionObserver as unknown as typeof IntersectionObserver;
});

afterEach(() => {
  // RTL's auto-cleanup only kicks in when vitest globals are enabled; this
  // project's vitest.config.ts does not set globals, so DOM from a previous
  // test leaks into the next render() and breaks placeholder-presence checks.
  cleanup();
});

describe("CodeEditor lazy mount (SKY-9051)", () => {
  test("renders a placeholder before the container intersects the viewport", () => {
    render(<CodeEditor value="print('hi')" language="python" />);

    // No CodeMirror DOM yet; placeholder reserves the layout slot.
    expect(document.querySelector(".cm-editor")).toBeNull();
    expect(
      document.querySelector('[data-codeeditor-state="pending"]'),
    ).not.toBeNull();
    // IntersectionObserver is observing the placeholder element.
    expect(observeMock).toHaveBeenCalledTimes(1);
  });

  test("mounts the editor once the container intersects the viewport", async () => {
    render(<CodeEditor value="print('hi')" language="python" />);

    expect(observerCallback).not.toBeNull();
    // Simulate viewport entry.
    observerCallback!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    await waitFor(() => {
      expect(document.querySelector(".cm-editor")).not.toBeNull();
    });
    // Observer is disconnected so subsequent layout changes don't re-fire.
    expect(disconnectMock).toHaveBeenCalled();
    expect(
      document.querySelector('[data-codeeditor-state="pending"]'),
    ).toBeNull();
  });

  test("mounts immediately when IntersectionObserver is unavailable", () => {
    // @ts-expect-error simulate environments without the API
    delete globalThis.IntersectionObserver;
    render(<CodeEditor value="print('hi')" language="python" />);

    expect(document.querySelector(".cm-editor")).not.toBeNull();
    expect(
      document.querySelector('[data-codeeditor-state="pending"]'),
    ).toBeNull();
  });

  test("keeps the editor mounted even after the container leaves the viewport", async () => {
    render(<CodeEditor value="print('hi')" language="python" />);
    observerCallback!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    await waitFor(() =>
      expect(document.querySelector(".cm-editor")).not.toBeNull(),
    );
    // Observer has already disconnected after first intersection, so a later
    // "isIntersecting:false" cannot be delivered. Asserting the disconnect
    // captures the "once mounted, stay mounted" contract.
    expect(disconnectMock).toHaveBeenCalled();
  });
});

describe("CodeEditor callback identity (SKY-9051)", () => {
  test("onCreateEditor and onUpdate references stay stable across re-renders", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <CodeEditor value="alpha" language="python" onChange={onChange} />,
    );

    expect(observerCallback).not.toBeNull();
    observerCallback!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    await waitFor(() => {
      expect(cmMockCalls.length).toBeGreaterThan(0);
    });

    const initialCallCount = cmMockCalls.length;
    const initialOnCreate = cmMockCalls[initialCallCount - 1]!.onCreateEditor;
    const initialOnUpdate = cmMockCalls[initialCallCount - 1]!.onUpdate;

    rerender(<CodeEditor value="beta" language="python" onChange={onChange} />);
    rerender(
      <CodeEditor value="gamma" language="python" onChange={onChange} />,
    );

    expect(cmMockCalls.length).toBeGreaterThan(initialCallCount);
    const finalOnCreate = cmMockCalls[cmMockCalls.length - 1]!.onCreateEditor;
    const finalOnUpdate = cmMockCalls[cmMockCalls.length - 1]!.onUpdate;

    expect(finalOnCreate).toBe(initialOnCreate);
    expect(finalOnUpdate).toBe(initialOnUpdate);
  });
});

describe("CodeEditor theme (SKY-12414)", () => {
  function mountAndReadTheme() {
    render(<CodeEditor value="print('hi')" language="python" />);
    observerCallback!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    return waitFor(() => {
      expect(cmMockCalls.length).toBeGreaterThan(0);
      return cmMockCalls[cmMockCalls.length - 1]!.theme;
    });
  }

  test("uses the light Tokyo Night Day theme in light mode", async () => {
    themeModeMock.mockReturnValue("light");
    expect(await mountAndReadTheme()).toBe(tokyoNightDay);
  });

  test("uses the dark Tokyo Night Storm theme in dark mode", async () => {
    themeModeMock.mockReturnValue("dark");
    expect(await mountAndReadTheme()).toBe(tokyoNightStorm);
  });
});
