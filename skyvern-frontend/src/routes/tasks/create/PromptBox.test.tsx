import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SVGProps,
  TextareaHTMLAttributes,
} from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PromptBox } from "./PromptBox";

const { mockNavigate, mockPost, mockSetAutoplay } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockPost: vi.fn(),
  mockSetAutoplay: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    post: mockPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

vi.mock("@/store/useAutoplayStore", () => ({
  useAutoplayStore: () => ({
    setAutoplay: mockSetAutoplay,
  }),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

vi.mock("@/components/AutoResizingTextarea/AutoResizingTextarea", () => ({
  AutoResizingTextarea: (
    props: TextareaHTMLAttributes<HTMLTextAreaElement>,
  ) => <textarea {...props} />,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/switch", () => ({
  Switch: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
}));

vi.mock("@/components/ProxySelector", () => ({
  ProxySelector: () => null,
}));

vi.mock("@/components/KeyValueInput", () => ({
  KeyValueInput: () => null,
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: () => null,
}));

vi.mock("@/components/TestWebhookDialog", () => ({
  TestWebhookDialog: ({ trigger }: { trigger: ReactNode }) => <>{trigger}</>,
}));

vi.mock("@/components/ImprovePrompt", () => ({
  ImprovePrompt: () => null,
}));

vi.mock("./ExampleCasePill", () => ({
  ExampleCasePill: ({
    label,
    onClick,
  }: {
    label: string;
    onClick: () => void;
  }) => (
    <button type="button" onClick={onClick}>
      {label}
    </button>
  ),
}));

vi.mock("@radix-ui/react-icons", () => ({
  FileTextIcon: () => null,
  GearIcon: () => null,
  Pencil1Icon: () => null,
  ReloadIcon: () => null,
  PaperPlaneIcon: (props: SVGProps<SVGSVGElement>) => <svg {...props} />,
}));

vi.mock("@/components/icons/CartIcon", () => ({ CartIcon: () => null }));
vi.mock("@/components/icons/GraphIcon", () => ({ GraphIcon: () => null }));
vi.mock("@/components/icons/InboxIcon", () => ({ InboxIcon: () => null }));
vi.mock("@/components/icons/MessageIcon", () => ({ MessageIcon: () => null }));
vi.mock("@/components/icons/TrophyIcon", () => ({ TrophyIcon: () => null }));

function renderPromptBox(enableCopilotHandoff = false) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <PromptBox enableCopilotHandoff={enableCopilotHandoff} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  mockNavigate.mockReset();
  mockPost.mockReset();
  mockSetAutoplay.mockReset();
});

describe("PromptBox", () => {
  test("creates prompt-generated workflows as V1 agent runs", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_1",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox();

    expect(screen.queryByText("Skyvern 2.0")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Visit the docs" },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const call = mockPost.mock.calls[0];
    expect(call).toBeDefined();
    const [path, body] = call!;

    expect(path).toBe("/workflows/create-from-prompt");
    expect(body.task_version).toBe("v1");
    expect(body.request.run_with).toBe("agent");
    expect(body.request.url).toBe("https://google.com");
  });

  test("hands Discover prompts to workflow copilot with agent execution", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_copilot",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(true);

    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Build this workflow" },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const call = mockPost.mock.calls[0];
    expect(call).toBeDefined();
    const [path, yaml] = call!;

    expect(path).toBe("/workflows");
    expect(yaml).toContain("run_with: agent");
    expect(yaml).not.toContain("run_with: code");
    expect(mockNavigate).toHaveBeenCalledWith(
      "/workflows/wpid_copilot/build?via=discover",
      {
        state: { copilotMessage: "Build this workflow" },
      },
    );
  });
});
