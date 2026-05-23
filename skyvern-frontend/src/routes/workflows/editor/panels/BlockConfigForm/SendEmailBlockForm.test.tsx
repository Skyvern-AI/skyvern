// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

const updateNodeDataMock = vi.fn();
const mockNodeFixtures = new Map<
  string,
  | {
      id: string;
      type: string;
      data: Record<string, unknown>;
    }
  | undefined
>();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: updateNodeDataMock,
    }),
    useNodes: () => Array.from(mockNodeFixtures.values()).filter(Boolean),
    useEdges: () => [],
  };
});

// The form imports `AppNode` and `isWorkflowBlockNode` from the nodes barrel,
// which transitively pulls every node-type component (heavy). Stub the
// surface the form actually consumes.
vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    node.type !== "nodeAdder" && node.type !== "start",
}));

// Field components have their own heavy dep chain (parameter autocomplete,
// popover, ghost text). The form test only verifies wiring — onChange goes
// through, value flows in. Replace the field components with thin DOM
// shims that surface those signals.
vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: ({
    value,
    onChange,
    placeholder,
    disabled,
  }: {
    value?: string;
    onChange: (v: string) => void;
    placeholder?: string;
    disabled?: boolean;
  }) => (
    <input
      placeholder={placeholder}
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
    placeholder,
  }: {
    value?: string;
    onChange: (v: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      placeholder={placeholder}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: ({ content }: { content: string }) => (
    <span data-testid="help-tooltip">{content}</span>
  ),
}));

const isFirstBlockMock = vi.fn<() => boolean>(() => false);
vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => isFirstBlockMock(),
}));

import { SendEmailBlockForm } from "./SendEmailBlockForm";

const baseSendEmailData = {
  editable: true,
  label: "Send Email",
  recipients: "alice@example.com",
  subject: "hello",
  body: "world",
  fileAttachments: "/downloads",
  sender: "noreply@skyvern.com",
  smtpHostSecretParameterKey: "SMTP_HOST",
  smtpPortSecretParameterKey: "SMTP_PORT",
  smtpUsernameSecretParameterKey: "SMTP_USERNAME",
  smtpPasswordSecretParameterKey: "SMTP_PASSWORD",
  continueOnFailure: false,
  model: null,
  debuggable: true,
};

beforeEach(() => {
  updateNodeDataMock.mockReset();
  mockNodeFixtures.clear();
  isFirstBlockMock.mockReset();
  isFirstBlockMock.mockImplementation(() => false);
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  cleanup();
});

describe("SendEmailBlockForm (SKY-9379)", () => {
  test("returns null when the node lookup misses", () => {
    mockNodeFixtures.set("missing", undefined);
    const { container } = render(<SendEmailBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a sendEmail block", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "task",
      data: baseSendEmailData,
    });
    const { container } = render(<SendEmailBlockForm blockId="b1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all four fields with values from node data", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    expect(screen.getByText("Recipients")).toBeDefined();
    expect(screen.getByText("Subject")).toBeDefined();
    expect(screen.getByText("Body")).toBeDefined();
    expect(screen.getByText("File Attachments")).toBeDefined();

    expect(
      screen.getByPlaceholderText("example@gmail.com, example2@gmail.com..."),
    ).toBeDefined();
    expect(screen.getByPlaceholderText("What is the gist?")).toBeDefined();
    expect(
      screen.getByPlaceholderText("What would you like to say?"),
    ).toBeDefined();
  });

  test("File Attachments input is disabled (beta parity)", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    const { container } = render(<SendEmailBlockForm blockId="b1" />);

    const inputs = Array.from(container.querySelectorAll("input"));
    const disabledInputs = inputs.filter((el) => el.disabled);
    expect(disabledInputs.length).toBeGreaterThanOrEqual(1);
  });

  test("Recipients onChange dispatches updateNodeData with new recipients", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    const input = screen.getByPlaceholderText(
      "example@gmail.com, example2@gmail.com...",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "bob@example.com" } });

    expect(updateNodeDataMock).toHaveBeenCalled();
    const lastCall =
      updateNodeDataMock.mock.calls[updateNodeDataMock.mock.calls.length - 1];
    expect(lastCall).toBeDefined();
    expect(lastCall![0]).toBe("b1");
    expect(lastCall![1]).toEqual({ recipients: "bob@example.com" });
  });

  test("Subject onChange dispatches updateNodeData with new subject", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    const input = screen.getByPlaceholderText(
      "What is the gist?",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new subject" } });

    const lastCall =
      updateNodeDataMock.mock.calls[updateNodeDataMock.mock.calls.length - 1];
    expect(lastCall).toBeDefined();
    expect(lastCall![0]).toBe("b1");
    expect(lastCall![1]).toEqual({ subject: "new subject" });
  });

  test("Body onChange dispatches updateNodeData with new body", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    const textarea = screen.getByPlaceholderText(
      "What would you like to say?",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "new body content" } });

    const lastCall =
      updateNodeDataMock.mock.calls[updateNodeDataMock.mock.calls.length - 1];
    expect(lastCall).toBeDefined();
    expect(lastCall![0]).toBe("b1");
    expect(lastCall![1]).toEqual({ body: "new body content" });
  });

  test("does not dispatch updateNodeData when editable=false", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: { ...baseSendEmailData, editable: false },
    });

    render(<SendEmailBlockForm blockId="b1" />);

    const input = screen.getByPlaceholderText(
      "What is the gist?",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "subject change" } });

    expect(updateNodeDataMock).not.toHaveBeenCalled();
  });

  test("registers a commit on mount and unregisters on unmount", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    const { unmount } = render(<SendEmailBlockForm blockId="b1" />);

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeDefined();
    expect(typeof usePendingCommitsStore.getState().commits["b1"]).toBe(
      "function",
    );

    unmount();

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeUndefined();
  });

  test("renders the 'Tip: Use the +' hint when block is the first in the workflow", () => {
    isFirstBlockMock.mockImplementation(() => true);
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    expect(
      screen.getByText(/Use the \+ button to add parameters/i),
    ).toBeDefined();
  });

  test("does NOT render the tip when block is not first", () => {
    isFirstBlockMock.mockImplementation(() => false);
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    expect(
      screen.queryByText(/Use the \+ button to add parameters/i),
    ).toBeNull();
  });
});
