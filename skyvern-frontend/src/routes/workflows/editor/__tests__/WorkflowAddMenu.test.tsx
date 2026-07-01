// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

import type { RadialMenuItem } from "@/components/RadialMenu";
import { DebugStoreContext } from "@/store/DebugStoreContext";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";

const radialMenuMock = vi.fn(({ children }: { children: React.ReactNode }) => (
  <div data-testid="radial-menu">{children}</div>
));

vi.mock("@/components/RadialMenu", () => ({
  RadialMenu: (props: { items: RadialMenuItem[]; children: React.ReactNode }) =>
    radialMenuMock(props),
}));

import { WorkflowAddMenu } from "../WorkflowAddMenu";

const initialSettings = useSettingsStore.getState();
const initialRecording = useRecordingStore.getState();

function renderInDebugMode() {
  return render(
    <DebugStoreContext.Provider
      value={{ isDebugMode: true, blockRunsEnabled: false }}
    >
      <WorkflowAddMenu
        onAdd={() => {}}
        onRecord={() => {}}
        onUploadSOP={() => {}}
      >
        <span>child</span>
      </WorkflowAddMenu>
    </DebugStoreContext.Provider>,
  );
}

function renderWithScope(scope: {
  isDebugMode: boolean;
  blockRunsEnabled: boolean;
}) {
  return render(
    <DebugStoreContext.Provider value={scope}>
      <WorkflowAddMenu
        onAdd={() => {}}
        onRecord={() => {}}
        onUploadSOP={() => {}}
      >
        <span>child</span>
      </WorkflowAddMenu>
    </DebugStoreContext.Provider>,
  );
}

function lastItems(): RadialMenuItem[] {
  const calls = (radialMenuMock as Mock).mock.calls;
  const last = calls[calls.length - 1];
  return (last?.[0]?.items as RadialMenuItem[]) ?? [];
}

function recordBrowserItem(items: RadialMenuItem[]) {
  return items.find((i) => i.text === "Record Browser");
}

describe("WorkflowAddMenu — Record Browser item visibility/enabled state", () => {
  beforeEach(() => {
    radialMenuMock.mockClear();
  });

  afterEach(() => {
    useSettingsStore.setState(initialSettings, true);
    useRecordingStore.setState(initialRecording, true);
    cleanup();
  });

  it("does NOT show the Record Browser item when no session is using or loading (CORR-1 regression guard)", () => {
    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(false);

    renderInDebugMode();

    expect(recordBrowserItem(lastItems())).toBeUndefined();
  });

  it("shows the Record Browser item as DISABLED while a session is loading", () => {
    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(true);

    renderInDebugMode();

    const item = recordBrowserItem(lastItems());
    expect(item).toBeDefined();
    expect(item?.enabled).toBe(false);
  });

  it("shows the Record Browser item as ENABLED once the browser is ready and not recording", () => {
    useSettingsStore.getState().setIsUsingABrowser(true);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useRecordingStore.setState({ isRecording: false });

    renderInDebugMode();

    const item = recordBrowserItem(lastItems());
    expect(item).toBeDefined();
    expect(item?.enabled).toBe(true);
  });

  it("disables the Record Browser item while recording is in progress, even when isUsingABrowser is true", () => {
    useSettingsStore.getState().setIsUsingABrowser(true);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useRecordingStore.setState({ isRecording: true });

    renderInDebugMode();

    const item = recordBrowserItem(lastItems());
    expect(item).toBeDefined();
    expect(item?.enabled).toBe(false);
  });
});

describe("WorkflowAddMenu — menu visibility gate", () => {
  beforeEach(() => {
    radialMenuMock.mockClear();
  });

  afterEach(() => {
    useSettingsStore.setState(initialSettings, true);
    useRecordingStore.setState(initialRecording, true);
    cleanup();
  });

  it("renders the radial menu in the studio editor (blockRunsEnabled) even when not in debug mode", () => {
    useSettingsStore.getState().setIsUsingABrowser(true);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useRecordingStore.setState({ isRecording: false });

    const { getByTestId } = renderWithScope({
      isDebugMode: false,
      blockRunsEnabled: true,
    });

    expect(getByTestId("radial-menu")).toBeDefined();
    const items = lastItems();
    expect(items.find((i) => i.text === "Upload SOP")).toBeDefined();
    expect(recordBrowserItem(items)).toBeDefined();
  });

  it("hides the menu (renders only children) when neither debug nor studio block-runs are active", () => {
    const { queryByTestId, getByText } = renderWithScope({
      isDebugMode: false,
      blockRunsEnabled: false,
    });

    expect(queryByTestId("radial-menu")).toBeNull();
    expect(getByText("child")).toBeDefined();
    expect(radialMenuMock).not.toHaveBeenCalled();
  });
});
