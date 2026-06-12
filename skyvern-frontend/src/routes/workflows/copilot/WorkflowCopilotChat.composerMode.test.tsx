import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeatureFlagContext,
  FeatureFlagValueContext,
} from "@/hooks/useFeatureFlag";

type StreamBody = {
  message: string;
  mode: string | null;
  code_block: boolean | null;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { streamCalls, postStreaming, cancelPost, historyResponse } = vi.hoisted(
  () => {
    const calls: StreamCall[] = [];
    const post = vi.fn().mockResolvedValue({});
    const streaming = vi.fn(
      (
        _path: string,
        body: StreamBody,
        onMessage: (payload: unknown) => boolean,
      ) =>
        new Promise<void>((resolve, reject) => {
          calls.push({ body, onMessage, resolve, reject });
        }),
    );
    const history = {
      data: {
        workflow_copilot_chat_id: null as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null as Record<string, unknown> | null,
        auto_accept: false,
      },
    };
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyResponse: history,
    };
  },
);

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation(() => Promise.resolve(historyResponse)),
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => ({
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined,
    }),
  };
});

const saveData = {
  title: "Test WF",
  workflow: {
    workflow_id: "wf_1",
    workflow_permanent_id: "wpid_1",
    description: "",
    totp_verification_url: null,
    is_saved_task: false,
    status: "published",
  },
  settings: {
    proxyLocation: null,
    webhookCallbackUrl: null,
    persistBrowserSession: false,
    browserProfileId: null,
    model: null,
    maxScreenshotScrolls: null,
    extraHttpHeaders: null,
    runWith: "agent",
    scriptCacheKey: "",
    aiFallback: true,
    codeVersion: 2,
    runSequentially: false,
    sequentialKey: null,
  },
  parameters: [],
  blocks: [],
  workflowDefinitionVersion: 1,
};

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

type FlagConfig = {
  copilotV2?: boolean;
  codeBlockMode?: boolean;
  defaultMode?: string;
};

async function renderChat(flags: FlagConfig) {
  const booleanFlags: Record<string, boolean> = {
    ENABLE_WORKFLOW_COPILOT_V2: flags.copilotV2 ?? false,
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: flags.codeBlockMode ?? false,
    CODE_BLOCK_ACCESS: flags.codeBlockMode ?? false,
  };
  const valueFlags: Record<string, string | undefined> = {
    WORKFLOW_COPILOT_DEFAULT_MODE: flags.defaultMode,
  };
  const view = render(
    <FeatureFlagContext.Provider value={(name) => booleanFlags[name]}>
      <FeatureFlagValueContext.Provider value={(name) => valueFlags[name]}>
        <WorkflowCopilotChat />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/Message Skyvern Copilot/)).toBeTruthy(),
  );
  return view;
}

function textarea(): HTMLTextAreaElement {
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

async function submit(value: string) {
  fireEvent.change(textarea(), { target: { value } });
  await act(async () => {
    fireEvent.keyDown(textarea(), { key: "Enter" });
  });
}

async function switchToBuild() {
  await act(async () => {
    fireEvent.pointerDown(screen.getByRole("button", { name: "Switch mode" }), {
      button: 0,
      ctrlKey: false,
    });
  });
  const buildItem = await screen.findByRole("menuitem", { name: /Build/ });
  await act(async () => {
    fireEvent.click(buildItem);
  });
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — composer default mode variant", () => {
  it("defaults to Build with code OFF when the variant is unset (new baseline)", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(false);
  });

  it("sends code_block=true for the build_code override variant", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      defaultMode: "build_code",
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(true);
  });

  it("sends code_block=false for the build_no_code variant", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      defaultMode: "build_no_code",
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(false);
  });

  it("defaults to Ask for the ask variant and sends code_block=null", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      defaultMode: "ask",
    });
    await submit("answer a question");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("ask");
    expect(streamCalls[0]?.body.code_block).toBe(null);
  });

  it("defaults to Ask for the ask_code variant and sends code_block=null while in Ask", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      defaultMode: "ask_code",
    });
    await submit("answer a question");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("ask");
    expect(streamCalls[0]?.body.code_block).toBe(null);
  });

  it("lands on code ON when ask_code switches to Build", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      defaultMode: "ask_code",
    });
    await switchToBuild();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(true);
  });

  it("sends code_block=null when the code-block flag is off, ignoring the variant", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: false,
      defaultMode: "build_no_code",
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(null);
  });
});
