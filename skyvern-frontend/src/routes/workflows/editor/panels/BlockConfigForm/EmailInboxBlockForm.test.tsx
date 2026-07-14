// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
    useNodes: () => [],
    useEdges: () => [],
  };
});

vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: () => true,
}));

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: () => <textarea />,
}));

vi.mock("@/routes/workflows/components/GoogleOAuthCredentialSelector", () => ({
  GoogleOAuthCredentialSelector: ({
    onChange,
  }: {
    onChange: (next: string) => void;
  }) => (
    <button
      data-testid="gmail-template-change"
      onClick={() => onChange("{{ gmail_credential_id }}")}
    />
  ),
}));

vi.mock(
  "@/routes/workflows/components/MicrosoftOAuthCredentialSelector",
  () => ({
    MicrosoftOAuthCredentialSelector: () => <div />,
    MICROSOFT_MAIL_REQUIRED_SCOPES: [],
  }),
);

vi.mock("@/components/ui/accordion", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Accordion: Pass,
    AccordionContent: Pass,
    AccordionItem: Pass,
    AccordionTrigger: Pass,
  };
});

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: ({
    parameters,
    onParametersChange,
  }: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
  }) => (
    <select
      data-testid="parameters-multi-select"
      multiple
      value={parameters}
      onChange={(event) =>
        onParametersChange(
          Array.from(event.target.selectedOptions, (option) => option.value),
        )
      }
    >
      <option value="microsoft_credential_id">microsoft_credential_id</option>
      <option value="gmail_credential_id">gmail_credential_id</option>
    </select>
  ),
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { emailInboxNodeDefaultData } from "../../nodes/EmailInboxNode/types";
import { EmailInboxBlockForm } from "./EmailInboxBlockForm";

beforeEach(() => {
  mockNodes.clear();
  updateNodeData.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  mockNodes.set("email-1", {
    id: "email-1",
    type: "emailInbox",
    data: {
      ...emailInboxNodeDefaultData,
      emailClient: "outlook",
      credentialId: "{{ microsoft_credential_id }}",
    },
  });
});

afterEach(() => {
  cleanup();
});

describe("EmailInboxBlockForm", () => {
  test("updates parameter keys used by a templated credential", () => {
    render(<EmailInboxBlockForm blockId="email-1" />);

    const select = screen.getByTestId(
      "parameters-multi-select",
    ) as HTMLSelectElement;
    const option = select.querySelector(
      'option[value="microsoft_credential_id"]',
    ) as HTMLOptionElement;
    option.selected = true;
    fireEvent.change(select);

    expect(updateNodeData).toHaveBeenCalledWith("email-1", {
      parameterKeys: ["microsoft_credential_id"],
    });
  });

  test("tracks the parameter key used by a Gmail credential template", () => {
    mockNodes.set("email-1", {
      id: "email-1",
      type: "emailInbox",
      data: {
        ...emailInboxNodeDefaultData,
        emailClient: "gmail",
      },
    });
    render(<EmailInboxBlockForm blockId="email-1" />);

    fireEvent.click(screen.getByTestId("gmail-template-change"));

    expect(updateNodeData).toHaveBeenCalledWith("email-1", {
      credentialId: "{{ gmail_credential_id }}",
      parameterKeys: ["gmail_credential_id"],
    });
  });

  test("replaces a stale credential parameter key", () => {
    mockNodes.set("email-1", {
      id: "email-1",
      type: "emailInbox",
      data: {
        ...emailInboxNodeDefaultData,
        emailClient: "gmail",
        credentialId: "{{ microsoft_credential_id }}",
        parameterKeys: ["microsoft_credential_id", "other_parameter"],
      },
    });
    render(<EmailInboxBlockForm blockId="email-1" />);

    fireEvent.click(screen.getByTestId("gmail-template-change"));

    expect(updateNodeData).toHaveBeenCalledWith("email-1", {
      credentialId: "{{ gmail_credential_id }}",
      parameterKeys: ["other_parameter", "gmail_credential_id"],
    });
  });

  test("preserves the previous credential key when another field uses it", () => {
    mockNodes.set("email-1", {
      id: "email-1",
      type: "emailInbox",
      data: {
        ...emailInboxNodeDefaultData,
        emailClient: "gmail",
        credentialId: "{{ microsoft_credential_id }}",
        folder: "{{ microsoft_credential_id }}",
        parameterKeys: ["microsoft_credential_id"],
      },
    });
    render(<EmailInboxBlockForm blockId="email-1" />);

    fireEvent.click(screen.getByTestId("gmail-template-change"));

    expect(updateNodeData).toHaveBeenCalledWith("email-1", {
      credentialId: "{{ gmail_credential_id }}",
      parameterKeys: ["microsoft_credential_id", "gmail_credential_id"],
    });
  });

  test.each([
    "{{ microsoft_credential_id.id }}",
    "{{ microsoft_credential_id | default('INBOX') }}",
  ])(
    "preserves the previous credential key for a suffixed reference: %s",
    (folder) => {
      mockNodes.set("email-1", {
        id: "email-1",
        type: "emailInbox",
        data: {
          ...emailInboxNodeDefaultData,
          emailClient: "gmail",
          credentialId: "{{ microsoft_credential_id }}",
          folder,
          parameterKeys: ["microsoft_credential_id"],
        },
      });
      render(<EmailInboxBlockForm blockId="email-1" />);

      fireEvent.click(screen.getByTestId("gmail-template-change"));

      expect(updateNodeData).toHaveBeenCalledWith("email-1", {
        credentialId: "{{ gmail_credential_id }}",
        parameterKeys: ["microsoft_credential_id", "gmail_credential_id"],
      });
    },
  );
});
