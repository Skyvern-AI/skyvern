// @vitest-environment jsdom

import { createElement, useLayoutEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSopToBlocksMutation } from "@/routes/workflows/hooks/useSopToBlocksMutation";
import { Edge } from "@xyflow/react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useRecordedBlocksStore,
  type RecordedParameter,
} from "@/store/RecordedBlocksStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import { AppNode } from "../nodes";
import { applyRecordedBlocksToGraph } from "./applyRecordedBlocksToGraph";
import { useApplyRecordedBlocks } from "./useApplyRecordedBlocks";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  commitYamlDraft,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  beginYamlCommit,
  registerEditorOwner,
  unregisterEditorOwner,
  runWorkflowAuthoringAction,
  createYamlCommitOwner,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowGraphState } from "../workflowEditorUtils";

const uploadMocks = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ post: uploadMocks.post }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

const initialRecordedBlocksState = useRecordedBlocksStore.getState();
const initialWorkflowParametersState = useWorkflowParametersStore.getState();

describe("useApplyRecordedBlocks", () => {
  beforeEach(() => {
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
  });
  afterEach(() => {
    cleanup();
    useRecordedBlocksStore.setState(initialRecordedBlocksState, true);
    useWorkflowParametersStore.setState(initialWorkflowParametersState, true);
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  it.each([true, false])(
    "records generated graph authorship before layout only when tracked: %s",
    (tracked) => {
      if (tracked)
        useWorkflowTitleStore.getState().startCopilotMetadata("wpid_test");
      const doLayout = vi.fn<(nodes: AppNode[], edges: Edge[]) => void>(() => {
        expect(
          useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_test
            ?.graphEdited,
        ).toBe(tracked ? true : undefined);
      });
      renderHook(() =>
        useApplyRecordedBlocks({
          enabled: true,
          nodes: [],
          edges: [],
          doLayout,
        }),
      );
      act(() =>
        useRecordedBlocksStore.getState().setRecordedBlocks(
          {
            blocks: [
              {
                block_type: "goto_url",
                label: "generated",
                url: "https://example.test",
              } as WorkflowBlock,
            ],
            parameters: [],
          },
          { previous: null, next: null, connectingEdgeType: "default" },
          useWorkflowYamlEditorStore.getState().editorOwner!,
        ),
      );
      expect(doLayout).toHaveBeenCalledOnce();
      expect(doLayout.mock.calls[0]![0]).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            data: expect.objectContaining({ label: "generated" }),
          }),
        ]),
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      if (!tracked)
        expect(useWorkflowTitleStore.getState().copilotMetadataEdits).toEqual(
          {},
        );
    },
  );

  it.each([
    "unmounted",
    "same workflow",
    "same workflow null anchors",
    "other workflow",
  ])(
    "binds a delayed SOP upload to its owner when the consumer is %s",
    async (remount) => {
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const wrapper = ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children);
      let resolveUpload!: (value: unknown) => void;
      uploadMocks.post.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveUpload = resolve;
          }),
      );
      const doLayout = vi.fn();
      const mountEditor = (workflowId: string) =>
        renderHook(
          () => {
            useLayoutEffect(() => {
              const owner = createYamlCommitOwner(workflowId);
              registerEditorOwner(owner);
              return () => unregisterEditorOwner(owner);
            }, []);
            const graph = useWorkflowGraphState(
              [
                {
                  id: workflowId + "-start",
                  type: "start",
                  position: { x: 0, y: 0 },
                  data: {},
                },
                {
                  id: workflowId + "-adder",
                  type: "nodeAdder",
                  position: { x: 0, y: 0 },
                  data: {},
                },
              ] as AppNode[],
              [
                {
                  id: "initial",
                  source: workflowId + "-start",
                  target: workflowId + "-adder",
                },
              ],
            );
            useApplyRecordedBlocks({
              enabled: true,
              nodes: graph.nodes,
              edges: graph.edges,
              doLayout: (nodes, edges) => {
                doLayout(nodes, edges);
                graph.setNodes(nodes);
                graph.setEdges(edges);
              },
            });
            const upload = useSopToBlocksMutation({
              onSuccess: (result, owner) =>
                useRecordedBlocksStore.getState().setRecordedBlocks(
                  result,
                  {
                    previous: remount.includes("null anchors")
                      ? null
                      : "disposed-start",
                    next: remount.includes("null anchors")
                      ? null
                      : "disposed-adder",
                    connectingEdgeType: "default",
                  },
                  owner,
                ),
            });
            return { graph, upload };
          },
          { wrapper },
        );
      const editor = mountEditor("wpid-1");
      let uploading!: Promise<boolean>;
      act(() => {
        uploading = runWorkflowAuthoringAction(() =>
          editor.result.current.upload.mutateAsync(
            new File(["pdf"], "steps.pdf"),
          ),
        );
      });
      await waitFor(() => expect(uploadMocks.post).toHaveBeenCalled());
      editor.unmount();
      const replacement =
        remount === "unmounted"
          ? null
          : mountEditor(
              remount.startsWith("same workflow") ? "wpid-1" : "wpid-2",
            );
      await act(async () => {
        resolveUpload({
          data: {
            blocks: [
              {
                block_type: "goto_url",
                label: "uploaded",
                url: "https://example.com",
              },
            ],
            parameters: [],
          },
        });
        await uploading;
      });
      expect(doLayout).toHaveBeenCalledTimes(
        remount.startsWith("same workflow") ? 1 : 0,
      );
      expect(useRecordedBlocksStore.getState().blocks).toBeNull();
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        authoringAction: null,
        authoringInProgress: false,
      });
      if (remount.startsWith("same workflow")) {
        expect(
          replacement!.result.current.graph.nodes.filter(
            (node) => node.data.label === "uploaded",
          ),
        ).toHaveLength(1);
        const { nodes, edges } = replacement!.result.current.graph;
        expect(edges).toHaveLength(2);
        expect(
          edges.every(
            (edge) =>
              nodes.some((node) => node.id === edge.source) &&
              nodes.some((node) => node.id === edge.target),
          ),
        ).toBe(true);
        replacement!.rerender();
        expect(doLayout).toHaveBeenCalledTimes(1);
      }
      const token = beginCopilotAcceptance();
      expect(token).not.toBeNull();
      finishCopilotAcceptance(token!);
      replacement?.unmount();
      client.clear();
      uploadMocks.post.mockClear();
    },
  );

  it("keeps generated blocks when an open YAML draft switches to Visual", async () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.open("blocks: []");
    const { result } = renderHook(() => {
      const graph = useWorkflowGraphState([], []);
      useApplyRecordedBlocks({
        enabled: true,
        nodes: graph.nodes,
        edges: graph.edges,
        doLayout: (nodes, edges) => {
          graph.setNodes(nodes);
          graph.setEdges(edges);
        },
      });
      return graph;
    });
    const commit = vi.fn(async () => {
      result.current.setNodes([]);
      return true;
    });
    store.registerCommit(commit);
    act(() =>
      useRecordedBlocksStore.getState().setRecordedBlocks(
        {
          blocks: [
            {
              block_type: "goto_url",
              label: "generated",
              url: "https://example.test",
            } as WorkflowBlock,
          ],
          parameters: [],
        },
        { previous: null, next: null, connectingEdgeType: "default" },
        store.editorOwner!,
      ),
    );
    expect(
      result.current.nodes.some((node) => node.data.label === "generated"),
    ).toBe(true);
    await act(async () => {
      expect(await commitYamlDraft(false)).toBe(false);
    });
    expect(commit).not.toHaveBeenCalled();
    expect(
      result.current.nodes.some((node) => node.data.label === "generated"),
    ).toBe(true);
  });

  it.each(["ownerless", "obsolete"])(
    "rejects %s generated blocks before touching the receiving canvas",
    (kind) => {
      const doLayout = vi.fn();
      const oldOwner = createYamlCommitOwner("wpid_old");
      useRecordedBlocksStore.setState({
        owner: kind === "ownerless" ? null : oldOwner,
        blocks: [
          {
            block_type: "goto_url",
            label: "foreign",
            url: "https://example.test",
          } as WorkflowBlock,
        ],
        parameters: [
          {
            key: "foreign",
            parameter_type: "workflow",
            workflow_parameter_type: "string",
          } as RecordedParameter,
        ],
        insertionPoint: {
          previous: "old-start",
          next: "old-adder",
          connectingEdgeType: "default",
        },
      });
      renderHook(() =>
        useApplyRecordedBlocks({
          enabled: true,
          nodes: [],
          edges: [],
          doLayout,
        }),
      );
      expect(doLayout).not.toHaveBeenCalled();
      expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
    },
  );

  it.each(["copilot", "yaml"])(
    "retains generated blocks during a %s lock and applies them once after unlock",
    (lock) => {
      const owner = createYamlCommitOwner("wpid-1");
      const token = lock === "copilot" ? beginCopilotAcceptance() : null;
      if (lock === "yaml") expect(beginYamlCommit(owner)).toBe(true);
      useWorkflowHasChangesStore.setState({ hasChanges: false });
      const doLayout = vi.fn();
      const { result, rerender } = renderHook(() => {
        const graph = useWorkflowGraphState([], []);
        useApplyRecordedBlocks({
          enabled: true,
          nodes: graph.nodes,
          edges: graph.edges,
          doLayout: (nodes, edges) => {
            doLayout(nodes, edges);
            graph.setNodes(nodes);
            graph.setEdges(edges);
          },
        });
        return graph;
      });
      act(() =>
        useRecordedBlocksStore.getState().setRecordedBlocks(
          {
            blocks: [
              {
                block_type: "goto_url",
                label: "uploaded",
                url: "https://example.com",
              } as WorkflowBlock,
            ],
            parameters: [
              {
                key: "upload_input",
                parameter_type: "workflow",
                workflow_parameter_type: "string",
                default_value: "",
                description: "",
              } as RecordedParameter,
            ],
          },
          { previous: null, next: null, connectingEdgeType: "default" },
          useWorkflowYamlEditorStore.getState().editorOwner!,
        ),
      );

      rerender();
      expect(doLayout).not.toHaveBeenCalled();
      expect(result.current.nodes).toHaveLength(0);
      expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
      expect(useRecordedBlocksStore.getState().blocks).toHaveLength(1);
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);

      act(() => {
        if (token) finishCopilotAcceptance(token);
        else finishYamlCommit(owner);
      });
      expect(
        result.current.nodes.filter((node) => node.data.label === "uploaded"),
      ).toHaveLength(1);
      expect(useWorkflowParametersStore.getState().parameters).toMatchObject([
        { key: "upload_input" },
      ]);
      expect(useRecordedBlocksStore.getState().blocks).toBeNull();
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        false,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(doLayout).toHaveBeenCalledTimes(1);
      rerender();
      expect(doLayout).toHaveBeenCalledTimes(1);
    },
  );

  it("applies recorded blocks when enabled in debugger/build mode", () => {
    const doLayout = vi.fn();
    const nodes = [{ id: "start", data: { label: "start" } }] as Array<AppNode>;
    const edges = [] as Array<Edge>;

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "action",
            label: "click_button",
            title: "Click button",
            navigation_goal: "Click the button.",
            url: null,
            parameters: [],
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes,
        edges,
        doLayout,
      }),
    );

    expect(doLayout).toHaveBeenCalledTimes(1);
    const layoutArgs = doLayout.mock.calls[0];
    expect(layoutArgs).toBeDefined();
    const [mergedNodes, mergedEdges] = layoutArgs!;
    expect(mergedNodes).toHaveLength(2);
    expect(mergedEdges.length).toBeGreaterThan(0);
  });

  it("uniquifies a recorded label already used by the workflow", () => {
    const existing = {
      id: "existing",
      type: "codeBlock",
      data: { label: "log_in" },
    } as AppNode;
    const recorded = {
      block_type: "code",
      label: "log_in",
      code: "await page.wait_for_timeout(1000)",
      prompt: "",
    } as unknown as WorkflowBlock;

    const result = applyRecordedBlocksToGraph({
      nodes: [existing],
      edges: [],
      recordedBlocks: [recorded],
      recordedInsertionPoint: {
        previous: null,
        next: null,
        connectingEdgeType: "default",
      },
      recordedParameters: [],
      existingParameters: [],
    });

    expect(result.nodes.map((node) => node.data.label)).toEqual([
      "log_in",
      "log_in_2",
    ]);
  });

  it("does not apply recorded blocks when disabled", () => {
    const doLayout = vi.fn();

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "goto_url",
            label: "goto_home",
            url: "https://example.com",
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: null,
        next: null,
        connectingEdgeType: "default",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: false,
        nodes: [],
        edges: [],
        doLayout,
      }),
    );

    expect(doLayout).not.toHaveBeenCalled();
  });

  it("clears a pending payload when the consumer unmounts (interrupted handoff)", () => {
    const doLayout = vi.fn();

    // enabled: false keeps the payload pending — mirrors a handoff that never
    // applied before the canvas went away (e.g. navigating right after commit).
    const { unmount } = renderHook(() =>
      useApplyRecordedBlocks({
        enabled: false,
        nodes: [],
        edges: [],
        doLayout,
      }),
    );

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "goto_url",
            label: "goto_home",
            url: "https://example.com",
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: null,
        next: null,
        connectingEdgeType: "default",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    unmount();

    const state = useRecordedBlocksStore.getState();
    expect(state.blocks).toBeNull();
    expect(state.parameters).toBeNull();
    expect(state.insertionPoint).toBeNull();
  });
  it("allocates the next free key when the workflow already owns credentials", () => {
    const doLayout = vi.fn();

    // A workflow whose existing login block picked a credential by hand already owns
    // `credentials`; the recorded credential must not shadow it.
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_already_here",
        },
      ],
    });

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "login",
            label: "type_password",
            url: "https://example.com/login",
            parameters: [{ key: "cred_just_recorded" }],
            parameter_keys: ["cred_just_recorded"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_just_recorded",
            parameter_type: "credential",
            credential_id: "cred_just_recorded",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const stored = useWorkflowParametersStore.getState().parameters;
    // The recorded credential survives under a fresh key instead of being dropped.
    expect(stored).toHaveLength(2);
    expect(stored).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          key: "credentials_1",
          credentialId: "cred_just_recorded",
        }),
        expect.objectContaining({
          key: "credentials",
          credentialId: "cred_already_here",
        }),
      ]),
    );

    // ...and the recorded login block points at it, not at the pre-existing credential.
    const [mergedNodes] = doLayout.mock.calls[0]!;
    const loginNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "login",
    );
    expect(loginNode?.data.parameterKeys).toEqual(["credentials_1"]);
  });

  it("reuses the existing key when the recorded credential is the same one", () => {
    const doLayout = vi.fn();

    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_same",
        },
      ],
    });

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "login",
            label: "type_password",
            url: "https://example.com/login",
            parameters: [{ key: "cred_same" }],
            parameter_keys: ["cred_same"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_same",
            parameter_type: "credential",
            credential_id: "cred_same",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    expect(useWorkflowParametersStore.getState().parameters).toHaveLength(1);
    const [mergedNodes] = doLayout.mock.calls[0]!;
    const loginNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "login",
    );
    expect(loginNode?.data.parameterKeys).toEqual(["credentials"]);
  });
  it("substitutes the allocated key into a secret fill's instruction", () => {
    const doLayout = vi.fn();

    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_already_here",
        },
      ],
    });

    // A recorded secret fill carries the key inside its instruction.
    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "action",
            label: "type_api_token",
            navigation_goal:
              "Type 'API token' with {{ cred_just_recorded.secret_value }}.",
            parameters: [{ key: "cred_just_recorded" }],
            parameter_keys: ["cred_just_recorded"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_just_recorded",
            parameter_type: "credential",
            credential_id: "cred_just_recorded",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const [mergedNodes] = doLayout.mock.calls[0]!;
    const actionNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "action",
    );
    // Declaration and reference move together, or the fill reads the other credential.
    expect(actionNode?.data.parameterKeys).toEqual(["credentials_1"]);
    expect(actionNode?.data.navigationGoal).toBe(
      "Type 'API token' with {{ credentials_1.secret_value }}.",
    );
  });
  it("substitutes the allocated key into a recorded code block's code", () => {
    const doLayout = vi.fn();

    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_already_here",
        },
      ],
    });

    // A code-first recording reads the credential through the token as an identifier.
    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "code",
            label: "recorded_example_com",
            code: 'await page.locator("#pw").fill(cred_just_recorded.password)',
            prompt: "",
            parameters: [{ key: "cred_just_recorded" }],
            parameter_keys: ["cred_just_recorded"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_just_recorded",
            parameter_type: "credential",
            credential_id: "cred_just_recorded",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const [mergedNodes] = doLayout.mock.calls[0]!;
    const codeNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "codeBlock",
    );
    // The code moves with the declaration, or the fill reads the other credential.
    expect(codeNode?.data.parameterKeys).toEqual(["credentials_1"]);
    expect(codeNode?.data.code).toBe(
      'await page.locator("#pw").fill(credentials_1.password)',
    );
  });
  it("does not allocate a key a recorded workflow parameter already claims", () => {
    const doLayout = vi.fn();

    // A field labelled "Credentials" mints the same key the credential allocator hands out.
    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "code",
            label: "recorded_example_com",
            code: 'await page.locator("#pw").fill(cred_recorded.password)',
            prompt: "",
            parameters: [{ key: "credentials" }, { key: "cred_recorded" }],
            parameter_keys: ["credentials", "cred_recorded"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          // The recording response carries only these fields, not a persisted WorkflowParameter.
          {
            key: "credentials",
            parameter_type: "workflow",
            workflow_parameter_type: "string",
            default_value: "",
            description: "",
          } as unknown as RecordedParameter,
          {
            key: "cred_recorded",
            parameter_type: "credential",
            credential_id: "cred_recorded",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const stored = useWorkflowParametersStore.getState().parameters;
    const keys = stored.map((parameter) => parameter.key);
    // Two parameters under one key make the workflow unsavable.
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain("credentials");
    const credential = stored.find((p) => p.parameterType === "credential");
    expect(credential?.key).not.toBe("credentials");
  });

  it("renames a recorded parameter that lands on a reused credential key", () => {
    const doLayout = vi.fn();

    // The workflow already wraps this credential under the default key.
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_same",
        },
      ],
    });

    // A field labelled "Credentials" mints the same key the wrapper already holds.
    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "code",
            label: "recorded_example_com",
            code:
              'await page.locator("#note").fill(str(credentials))\n' +
              'await page.locator("#pw").fill(cred_same.password)',
            prompt: "",
            parameters: [{ key: "credentials" }, { key: "cred_same" }],
            parameter_keys: ["credentials", "cred_same"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "credentials",
            parameter_type: "workflow",
            workflow_parameter_type: "string",
            default_value: "",
            description: "",
          } as unknown as RecordedParameter,
          {
            key: "cred_same",
            parameter_type: "credential",
            credential_id: "cred_same",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const [mergedNodes] = doLayout.mock.calls[0]!;
    const codeNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "codeBlock",
    );
    // Sharing the key would make str(credentials) stringify the credential, password included,
    // into the page field.
    expect(codeNode?.data.code).toContain('#note").fill(str(credentials_2))');
    expect(codeNode?.data.code).toContain('#pw").fill(credentials.password)');
    const stored = useWorkflowParametersStore.getState().parameters;
    expect(stored.map((parameter) => parameter.key).sort()).toEqual([
      "credentials",
      "credentials_2",
    ]);
  });

  it("substitutes tokens in one pass when an allocated key is another token", () => {
    const doLayout = vi.fn();

    // The user named their own credential parameter after a credential id.
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "cred_second",
          parameterType: "credential",
          credentialId: "cred_first",
        },
      ],
    });

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "code",
            label: "recorded_example_com",
            code:
              'await page.locator("#a").fill(cred_first.password)\n' +
              'await page.locator("#b").fill(cred_second.password)',
            prompt: "",
            parameters: [{ key: "cred_first" }, { key: "cred_second" }],
            parameter_keys: ["cred_first", "cred_second"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_first",
            parameter_type: "credential",
            credential_id: "cred_first",
            description: "",
          },
          {
            key: "cred_second",
            parameter_type: "credential",
            credential_id: "cred_second",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const [mergedNodes] = doLayout.mock.calls[0]!;
    const codeNode = (mergedNodes as Array<AppNode>).find(
      (node) => node.type === "codeBlock",
    );
    // Cascading passes would rewrite the first fill twice and point both at cred_second.
    expect(codeNode?.data.code).toContain('#a").fill(cred_second.password)');
    expect(codeNode?.data.code).toContain('#b").fill(credentials.password)');
  });

  it("gives two recorded logins distinct keys over an existing credentials key", () => {
    const doLayout = vi.fn();

    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_pre_existing",
        },
      ],
    });

    // The recorder keys each login by its credential id.
    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "login",
            label: "login_one",
            parameters: [{ key: "cred_a" }],
            parameter_keys: ["cred_a"],
          } as unknown as WorkflowBlock,
          {
            block_type: "login",
            label: "login_two",
            parameters: [{ key: "cred_b" }],
            parameter_keys: ["cred_b"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_a",
            parameter_type: "credential",
            credential_id: "cred_a",
            description: "",
          },
          {
            key: "cred_b",
            parameter_type: "credential",
            credential_id: "cred_b",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    const stored = useWorkflowParametersStore.getState().parameters;
    const byCredential = new Map(
      stored
        .filter((p) => "credentialId" in p)
        .map((p) => [(p as { credentialId: string }).credentialId, p.key]),
    );
    // Distinct credentials must not collapse onto one key, or the second login
    // authenticates as the first.
    expect(byCredential.get("cred_a")).toBe("credentials_1");
    expect(byCredential.get("cred_b")).toBe("credentials_2");

    const [mergedNodes] = doLayout.mock.calls[0]!;
    const loginKeys = (mergedNodes as Array<AppNode>)
      .filter((node) => node.type === "login")
      .map(
        (node) => (node.data as { parameterKeys: Array<string> }).parameterKeys,
      );
    expect(loginKeys).toEqual([["credentials_1"], ["credentials_2"]]);
  });
  it("keys the first recorded credential on a fresh workflow as credentials", () => {
    const doLayout = vi.fn();
    useWorkflowParametersStore.setState({ parameters: [] });

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "login",
            label: "login",
            parameters: [{ key: "cred_a" }],
            parameter_keys: ["cred_a"],
          } as unknown as WorkflowBlock,
        ],
        parameters: [
          {
            key: "cred_a",
            parameter_type: "credential",
            credential_id: "cred_a",
            description: "",
          },
        ],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
      useWorkflowYamlEditorStore.getState().editorOwner!,
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes: [{ id: "start", data: { label: "start" } }] as Array<AppNode>,
        edges: [] as Array<Edge>,
        doLayout,
      }),
    );

    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      expect.objectContaining({ key: "credentials", credentialId: "cred_a" }),
    ]);
    const [mergedNodes] = doLayout.mock.calls[0]!;
    const loginNode = (mergedNodes as Array<AppNode>).find(
      (n) => n.type === "login",
    );
    expect(loginNode?.data.parameterKeys).toEqual(["credentials"]);
  });
});
