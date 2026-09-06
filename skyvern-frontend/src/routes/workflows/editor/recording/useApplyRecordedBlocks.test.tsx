// @vitest-environment jsdom

import { Edge } from "@xyflow/react";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import { AppNode } from "../nodes";
import { useApplyRecordedBlocks } from "./useApplyRecordedBlocks";

const initialRecordedBlocksState = useRecordedBlocksStore.getState();
const initialWorkflowParametersState = useWorkflowParametersStore.getState();

describe("useApplyRecordedBlocks", () => {
  afterEach(() => {
    useRecordedBlocksStore.setState(initialRecordedBlocksState, true);
    useWorkflowParametersStore.setState(initialWorkflowParametersState, true);
  });

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
