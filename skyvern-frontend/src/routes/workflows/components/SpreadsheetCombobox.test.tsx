// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpreadsheetCombobox } from "./SpreadsheetCombobox";

const mocks = vi.hoisted(() => ({
  capture: vi.fn(),
  useCreateGoogleSpreadsheet: vi.fn(),
  useGoogleSpreadsheets: vi.fn(),
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

vi.mock("@/hooks/useGoogleSpreadsheets", () => ({
  useGoogleSpreadsheets: mocks.useGoogleSpreadsheets,
}));
vi.mock("@/hooks/useCreateGoogleSpreadsheet", () => ({
  useCreateGoogleSpreadsheet: mocks.useCreateGoogleSpreadsheet,
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
  overrides: Partial<ComponentProps<typeof SpreadsheetCombobox>> = {},
) {
  const props: ComponentProps<typeof SpreadsheetCombobox> = {
    nodeId: "node-1",
    credentialId: "credential-1",
    hasSelectedAccount: true,
    value: "",
    displayName: null,
    placeholder: "Select a spreadsheet",
    allowCreate: false,
    blockType: "google_sheets_write",
    templateMode: false,
    onTemplateModeChange: vi.fn(),
    onChange: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  };

  render(<SpreadsheetCombobox {...props} />);
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useGoogleSpreadsheets.mockReturnValue({
    data: {
      pages: [
        {
          spreadsheets: [
            {
              id: "spreadsheet_test_id_123",
              name: "Budget Plan",
              modified_time: null,
            },
          ],
        },
      ],
    },
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    isLoading: false,
  });
  mocks.useCreateGoogleSpreadsheet.mockReturnValue({
    isPending: false,
    mutateAsync: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
});

describe("SpreadsheetCombobox", () => {
  it("opens the picker on focus with list content and the template footer", () => {
    renderCombobox();

    fireEvent.focus(screen.getByRole("textbox"));

    expect(screen.getByTestId("popover-content").dataset.open).toBe("true");
    expect(screen.getByText("Budget Plan")).toBeTruthy();
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

  it("suppresses the picker and renders the raw value in controlled template mode", () => {
    renderCombobox({
      displayName: "Budget Plan",
      value: spreadsheetUrl,
      templateMode: true,
    });

    expect(screen.getByRole<HTMLTextAreaElement>("textbox").value).toBe(
      spreadsheetUrl,
    );
    expect(screen.getByRole("textbox").getAttribute("placeholder")).toBe(
      "{{ target_spreadsheet_url }}",
    );

    fireEvent.focus(screen.getByRole("textbox"));
    expect(screen.getByTestId("popover-content").dataset.open).toBe("false");
  });

  it("suppresses the picker for template values when controlled mode is false", () => {
    renderCombobox({ value: "{{ sheet_url }}", templateMode: false });
    fireEvent.focus(screen.getByRole("textbox"));

    expect(screen.getByTestId("popover-content").dataset.open).toBe("false");
  });

  it("never searches for a template expression", () => {
    renderCombobox({
      value: "{{ target_spreadsheet_url }}",
      templateMode: false,
    });

    expect(mocks.useGoogleSpreadsheets).toHaveBeenCalled();
    expect(
      mocks.useGoogleSpreadsheets.mock.calls.some(
        ([options]) =>
          typeof options.query === "string" && options.query.includes("{{"),
      ),
    ).toBe(false);
    expect(mocks.useGoogleSpreadsheets).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: "" }),
    );
  });
});
