// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockNodes = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeData = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodes.get(id),
      updateNodeData,
    }),
    useNodesData: (id: string) => mockNodes.get(id),
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
  };
});

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <input
      data-testid={`wbi-${props.placeholder ?? "input"}`}
      placeholder={props.placeholder}
      value={props.value ?? ""}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      data-testid="wbi-textarea-body"
      placeholder={props.placeholder}
      value={props.value ?? ""}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: {
    value?: string | number;
    onChange?: (event: React.ChangeEvent<HTMLInputElement>) => void;
    placeholder?: string;
    className?: string;
    disabled?: boolean;
  }) => (
    <input
      data-testid="timeout-minutes-input"
      placeholder={props.placeholder}
      value={props.value ?? ""}
      disabled={props.disabled}
      onChange={(event) => props.onChange?.(event)}
    />
  ),
}));

// Force the Accordion content to always render so we can test advanced
// settings without needing to click the trigger. Mirrors the strategy from
// the ValidationBlockForm test.
vi.mock("@/components/ui/accordion", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Accordion: Pass,
    AccordionItem: Pass,
    AccordionTrigger: ({ children }: { children?: ReactNode }) => (
      <button data-testid="accordion-trigger">{children}</button>
    ),
    AccordionContent: Pass,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { HumanInteractionBlockForm } from "./HumanInteractionBlockForm";

beforeEach(() => {
  vi.useFakeTimers();
  mockNodes.clear();
  updateNodeData.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

function setHumanInteractionNode(
  id: string,
  overrides: Partial<{
    instructions: string;
    timeoutSeconds: number;
    recipients: string;
    subject: string;
    body: string;
    negativeDescriptor: string;
    positiveDescriptor: string;
    editable: boolean;
  }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "human_interaction",
    data: {
      instructions:
        overrides.instructions ??
        "Please review and approve or reject to continue the workflow.",
      timeoutSeconds: overrides.timeoutSeconds ?? 7200,
      recipients: overrides.recipients ?? "",
      subject:
        overrides.subject ?? "Human interaction required for workflow run",
      body:
        overrides.body ?? "Your interaction is required for a workflow run!",
      negativeDescriptor: overrides.negativeDescriptor ?? "Reject",
      positiveDescriptor: overrides.positiveDescriptor ?? "Approve",
      editable: overrides.editable ?? true,
      label: "human_interaction_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
      sender: "noreply@skyvern.com",
    },
  });
}

describe("HumanInteractionBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(
      <HumanInteractionBlockForm blockId="missing" />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("h1", { id: "h1", type: "task", data: {} });
    const { container } = render(<HumanInteractionBlockForm blockId="h1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all 7 editable fields with current node data", () => {
    setHumanInteractionNode("h1", {
      instructions: "Please confirm.",
      timeoutSeconds: 600,
      recipients: "alice@example.com",
      subject: "Hi",
      body: "Body content",
      negativeDescriptor: "No",
      positiveDescriptor: "Yes",
    });
    render(<HumanInteractionBlockForm blockId="h1" />);

    expect(screen.getByText("Instructions For Human")).toBeDefined();
    expect(screen.getByText("Timeout (minutes)")).toBeDefined();
    expect(screen.getByText("Email Settings")).toBeDefined();
    expect(screen.getByText("Recipients")).toBeDefined();
    expect(screen.getByText("Subject")).toBeDefined();
    expect(screen.getByText("Body")).toBeDefined();
    expect(screen.getByText("Negative Button Label")).toBeDefined();
    expect(screen.getByText("Positive Button Label")).toBeDefined();

    expect(
      (
        screen.getByPlaceholderText(
          "Please review and approve or reject to continue the workflow.",
        ) as HTMLInputElement
      ).value,
    ).toBe("Please confirm.");
    expect(
      (screen.getByTestId("timeout-minutes-input") as HTMLInputElement).value,
    ).toBe("10");
    expect(
      (
        screen.getByPlaceholderText(
          "example@gmail.com, example2@gmail.com...",
        ) as HTMLInputElement
      ).value,
    ).toBe("alice@example.com");
    expect(
      (
        screen.getByPlaceholderText(
          "Human interaction required for workflow run",
        ) as HTMLInputElement
      ).value,
    ).toBe("Hi");
    expect(
      (
        screen.getByPlaceholderText(
          "Your interaction is required for a workflow run!",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("Body content");
    expect(
      (screen.getByPlaceholderText("Reject") as HTMLInputElement).value,
    ).toBe("No");
    expect(
      (screen.getByPlaceholderText("Approve") as HTMLInputElement).value,
    ).toBe("Yes");
  });

  test("editing instructions propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(
      screen.getByPlaceholderText(
        "Please review and approve or reject to continue the workflow.",
      ),
      { target: { value: "Please double-check." } },
    );

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      instructions: "Please double-check.",
    });
  });

  test("editing timeoutSeconds (in minutes) writes timeoutSeconds * 60", () => {
    setHumanInteractionNode("h1", { timeoutSeconds: 7200 });
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(screen.getByTestId("timeout-minutes-input"), {
      target: { value: "30" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      timeoutSeconds: 1800,
    });
  });

  test("editing recipients propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(
      screen.getByPlaceholderText("example@gmail.com, example2@gmail.com..."),
      { target: { value: "bob@example.com" } },
    );

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      recipients: "bob@example.com",
    });
  });

  test("editing subject propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(
      screen.getByPlaceholderText(
        "Human interaction required for workflow run",
      ),
      { target: { value: "New subject" } },
    );

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      subject: "New subject",
    });
  });

  test("editing body propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(
      screen.getByPlaceholderText(
        "Your interaction is required for a workflow run!",
      ),
      { target: { value: "Updated body" } },
    );

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      body: "Updated body",
    });
  });

  test("editing negativeDescriptor propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(screen.getByPlaceholderText("Reject"), {
      target: { value: "No way" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      negativeDescriptor: "No way",
    });
  });

  test("editing positiveDescriptor propagates", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(screen.getByPlaceholderText("Approve"), {
      target: { value: "Yes please" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      positiveDescriptor: "Yes please",
    });
  });

  test("non-editable: timeoutSeconds change does not propagate", () => {
    setHumanInteractionNode("h1", { editable: false });
    render(<HumanInteractionBlockForm blockId="h1" />);

    fireEvent.change(screen.getByTestId("timeout-minutes-input"), {
      target: { value: "5" },
    });
    fireEvent.change(
      screen.getByPlaceholderText(
        "Please review and approve or reject to continue the workflow.",
      ),
      { target: { value: "blocked" } },
    );

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setHumanInteractionNode("h1");
    const { unmount } = render(<HumanInteractionBlockForm blockId="h1" />);
    expect(usePendingCommitsStore.getState().commits["h1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["h1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setHumanInteractionNode("h1");
    render(<HumanInteractionBlockForm blockId="h1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("h1");
    });
    expect(ok).toBe(true);
  });
});
