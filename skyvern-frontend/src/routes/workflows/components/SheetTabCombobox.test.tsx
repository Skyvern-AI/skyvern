// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SheetTabCombobox } from "./SheetTabCombobox";

const mocks = vi.hoisted(() => ({
  capture: vi.fn(),
  useCreateGoogleSheetTab: vi.fn(),
  useGoogleSheetTabs: vi.fn(),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
    onFocus,
    placeholder,
  }: {
    value: string;
    onChange: (value: string) => void;
    onFocus?: () => void;
    placeholder?: string;
  }) => (
    <textarea
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onFocus={onFocus}
      placeholder={placeholder}
    />
  ),
}));

vi.mock("@/components/ui/popover", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  const OpenContext = React.createContext(false);

  return {
    Popover: ({ open, children }: { open?: boolean; children?: ReactNode }) => (
      <OpenContext.Provider value={Boolean(open)}>
        {children}
      </OpenContext.Provider>
    ),
    PopoverAnchor: ({ children }: { children?: ReactNode }) => <>{children}</>,
    PopoverContent: ({ children }: { children?: ReactNode }) => (
      <div
        data-testid="popover-content"
        data-open={String(React.useContext(OpenContext))}
      >
        {children}
      </div>
    ),
  };
});

vi.mock("@/hooks/useGoogleSheetTabs", () => ({
  useGoogleSheetTabs: mocks.useGoogleSheetTabs,
}));
vi.mock("@/hooks/useCreateGoogleSheetTab", () => ({
  useCreateGoogleSheetTab: mocks.useCreateGoogleSheetTab,
}));
vi.mock("@/hooks/useCurrentOrgId", () => ({
  useCurrentOrgId: () => "org_test",
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: mocks.capture }),
}));

const spreadsheetUrl =
  "https://docs.google.com/spreadsheets/d/spreadsheet_test_id_123/edit";

function renderCombobox(
  overrides: Partial<ComponentProps<typeof SheetTabCombobox>> = {},
) {
  const props: ComponentProps<typeof SheetTabCombobox> = {
    nodeId: "node-1",
    credentialId: "credential-1",
    hasSelectedAccount: true,
    spreadsheetUrl,
    value: "",
    placeholder: "Select a sheet",
    allowCreate: false,
    blockType: "google_sheets_read",
    templateMode: false,
    onTemplateModeChange: vi.fn(),
    onChange: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  };

  render(<SheetTabCombobox {...props} />);
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useGoogleSheetTabs.mockReturnValue({
    data: [
      {
        sheet_id: 1,
        title: "Summary",
      },
    ],
    error: null,
    isFetching: false,
    isLoading: false,
  });
  mocks.useCreateGoogleSheetTab.mockReturnValue({
    isPending: false,
    mutateAsync: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
});

describe("SheetTabCombobox", () => {
  it("opens the picker on focus with list content and the template footer", () => {
    renderCombobox();

    fireEvent.focus(screen.getByRole("textbox"));

    expect(screen.getByTestId("popover-content").dataset.open).toBe("true");
    expect(screen.getByText("Summary")).toBeTruthy();
    expect(screen.getByText("Use template expression")).toBeTruthy();
  });

  it("requests template mode from the footer and closes the popover", () => {
    const onChange = vi.fn();
    const onTemplateModeChange = vi.fn();
    renderCombobox({ onChange, onTemplateModeChange });
    fireEvent.focus(screen.getByRole("textbox"));

    fireEvent.click(screen.getByText("Use template expression"));

    expect(onTemplateModeChange).toHaveBeenCalledWith(true);
    expect(screen.getByTestId("popover-content").dataset.open).toBe("false");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("suppresses the picker in controlled template mode", () => {
    renderCombobox({ templateMode: true });

    expect(screen.getByRole("textbox").getAttribute("placeholder")).toBe(
      "sheet_{{ current_index }}",
    );
    fireEvent.focus(screen.getByRole("textbox"));
    expect(screen.getByTestId("popover-content").dataset.open).toBe("false");
  });

  it("suppresses the picker for template values when controlled mode is false", () => {
    renderCombobox({ value: "{{ sheet_name }}", templateMode: false });
    fireEvent.focus(screen.getByRole("textbox"));

    expect(screen.getByTestId("popover-content").dataset.open).toBe("false");
  });
});
