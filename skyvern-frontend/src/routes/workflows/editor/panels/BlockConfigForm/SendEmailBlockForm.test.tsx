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
    useNodesData: (id: string) => mockNodeFixtures.get(id),
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

vi.mock("@/routes/workflows/components/GoogleOAuthCredentialSelector", () => ({
  GoogleOAuthCredentialSelector: ({
    onChange,
  }: {
    onChange: (id: string) => void;
  }) => (
    <select
      aria-label="Gmail account"
      defaultValue="native_old"
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">No Google account</option>
      <option value="native_old">Original account</option>
      <option value="native_new">Another account</option>
    </select>
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
  bodyFormat: "text",
  fileAttachments: "/downloads",
  sender: "noreply@skyvern.com",
  smtpHostSecretParameterKey: "SMTP_HOST",
  smtpPortSecretParameterKey: "SMTP_PORT",
  smtpUsernameSecretParameterKey: "SMTP_USERNAME",
  smtpPasswordSecretParameterKey: "SMTP_PASSWORD",
  continueOnFailure: false,
  model: null,
  debuggable: true,
  customSmtpHost: null,
  customSmtpPort: null,
  customSmtpUsername: null,
  customSmtpPassword: null,
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
  test("preserves reply when choosing an account after the incompatible account was cleared", () => {
    const data = {
      ...baseSendEmailData,
      transport: "gmail",
      credentialId: "",
      replyToMessageId: "{{ email.id }}",
    };
    mockNodeFixtures.set("b1", { id: "b1", type: "sendEmail", data });
    render(<SendEmailBlockForm blockId="b1" />);
    fireEvent.change(screen.getByRole("combobox", { name: "Gmail account" }), {
      target: { value: "native_new" },
    });
    Object.assign(data, updateNodeDataMock.mock.lastCall?.[1]);
    expect(data.credentialId).toBe("native_new");
    expect(data.replyToMessageId).toBe("{{ email.id }}");
  });

  test("keeps reply enabled when its incompatible account is cleared", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: {
        ...baseSendEmailData,
        transport: "gmail",
        credentialId: "native_old",
        replyToMessageId: "{{ email.id }}",
      },
    });
    render(<SendEmailBlockForm blockId="b1" />);
    fireEvent.change(screen.getByRole("combobox", { name: "Gmail account" }), {
      target: { value: "" },
    });
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      credentialId: "",
      credentialSource: "native",
      connectionId: "",
    });
  });

  test("preserves the reply target when reselecting the same Gmail account", () => {
    const data = {
      ...baseSendEmailData,
      transport: "gmail",
      credentialId: "native_old",
      replyToMessageId: "synthetic_message",
    };
    mockNodeFixtures.set("b1", { id: "b1", type: "sendEmail", data });
    render(<SendEmailBlockForm blockId="b1" />);
    fireEvent.change(screen.getByRole("combobox", { name: "Gmail account" }), {
      target: { value: "native_old" },
    });
    Object.assign(data, updateNodeDataMock.mock.lastCall?.[1]);
    expect(data.replyToMessageId).toBe("synthetic_message");
  });

  test.each(["{{ email.id }}", "synthetic_message"])(
    "clears reply target %s when changing Gmail accounts",
    (replyToMessageId) => {
      mockNodeFixtures.set("b1", {
        id: "b1",
        type: "sendEmail",
        data: {
          ...baseSendEmailData,
          transport: "gmail",
          credentialId: "native_old",
          replyToMessageId,
        },
      });
      render(<SendEmailBlockForm blockId="b1" />);
      fireEvent.change(
        screen.getByRole("combobox", { name: "Gmail account" }),
        { target: { value: "native_new" } },
      );
      expect(updateNodeDataMock).toHaveBeenCalledWith(
        "b1",
        expect.objectContaining({
          credentialId: "native_new",
          replyToMessageId: "",
        }),
      );
    },
  );

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
    expect(
      screen.getByPlaceholderText("Your Run is Finished {{workflow_run_id}}"),
    ).toBeDefined();
    expect(
      screen.getByPlaceholderText("What would you like to say?"),
    ).toBeDefined();
    expect(
      screen.getByRole("combobox", { name: "Body format" }).textContent,
    ).toBe("Text");
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
      "Your Run is Finished {{workflow_run_id}}",
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
      "Your Run is Finished {{workflow_run_id}}",
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

    expect(screen.getByText(/Use the \+ button to add inputs/i)).toBeDefined();
  });

  test("does NOT render the tip when block is not first", () => {
    isFirstBlockMock.mockImplementation(() => false);
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    expect(screen.queryByText(/Use the \+ button to add inputs/i)).toBeNull();
  });

  test("Advanced section is collapsed by default and reveals SMTP fields on expand (SKY-14062)", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);

    expect(screen.queryByPlaceholderText("smtp.example.com")).toBeNull();

    fireEvent.click(screen.getByText("Advanced Settings"));

    expect(screen.getByPlaceholderText("smtp.example.com")).toBeDefined();
    expect(screen.getByText("SMTP Host")).toBeDefined();
    expect(screen.getByText("SMTP Port")).toBeDefined();
    expect(screen.getByText("SMTP Username")).toBeDefined();
    expect(screen.getByText("SMTP Password")).toBeDefined();
    expect(screen.getByText("Sender (From)")).toBeDefined();
  });

  test("SMTP host onChange dispatches updateNodeData with customSmtpHost (SKY-14062)", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);
    fireEvent.click(screen.getByText("Advanced Settings"));

    const input = screen.getByPlaceholderText(
      "smtp.example.com",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "smtp.gmail.com" } });

    const lastCall =
      updateNodeDataMock.mock.calls[updateNodeDataMock.mock.calls.length - 1];
    expect(lastCall).toBeDefined();
    expect(lastCall![0]).toBe("b1");
    expect(lastCall![1]).toEqual({ customSmtpHost: "smtp.gmail.com" });
  });

  test("SMTP port input strips non-numeric characters (SKY-14062)", () => {
    mockNodeFixtures.set("b1", {
      id: "b1",
      type: "sendEmail",
      data: baseSendEmailData,
    });

    render(<SendEmailBlockForm blockId="b1" />);
    fireEvent.click(screen.getByText("Advanced Settings"));

    const input = screen.getByPlaceholderText("587") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "58a7" } });

    const lastCall =
      updateNodeDataMock.mock.calls[updateNodeDataMock.mock.calls.length - 1];
    expect(lastCall).toBeDefined();
    expect(lastCall![1]).toEqual({ customSmtpPort: "587" });
  });
});
