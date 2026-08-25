import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import type {
  CredentialApiResponse,
  OnePasswordItemApiResponse,
} from "@/api/types";
import CloudContext from "@/store/CloudContext";
import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { CREDENTIAL_FALLBACK_RETRY_FLAG } from "@/util/featureFlags";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { LoginBlockCredentialSelector } from "./LoginBlockCredentialSelector";
import type { WorkflowStartNodeData } from "../StartNode/types";

const mocks = vi.hoisted(() => ({
  credentialDetail: {
    data: undefined as CredentialApiResponse | undefined,
    error: null as unknown,
    isError: false,
    isPending: false,
  },
  credentialDetailsById: new Map<string, CredentialApiResponse>(),
  searchValues: [] as Array<string | undefined>,
  useCredentialsQuery: vi.fn(),
  onePasswordItems: {
    data: undefined as
      | { configured: boolean; items: Array<OnePasswordItemApiResponse> }
      | undefined,
    isLoading: false,
    isError: false,
  },
  nodes: [] as Array<{
    id: string;
    type: "start";
    data: WorkflowStartNodeData;
  }>,
  updateNodeData: vi.fn(),
}));

vi.mock("@xyflow/react", () => ({
  useNodes: () => mocks.nodes,
  useReactFlow: () => ({ updateNodeData: mocks.updateNodeData }),
}));
vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: mocks.useCredentialsQuery,
}));
vi.mock("@/routes/workflows/hooks/useCredentialQuery", () => ({
  isCredentialNotFoundError: (error: unknown) =>
    (error as { status?: number } | null)?.status === 404,
  useCredentialQuery: (credentialId: string | undefined) => ({
    ...mocks.credentialDetail,
    data:
      (credentialId && mocks.credentialDetailsById.get(credentialId)) ??
      mocks.credentialDetail.data,
  }),
}));
vi.mock("@/routes/workflows/hooks/useOnePasswordItemsQuery", () => ({
  useOnePasswordItemsQuery: () => mocks.onePasswordItems,
}));
vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: () => null,
}));
vi.mock("@/routes/credentials/useCredentialModalState", () => ({
  CredentialModalTypes: { PASSWORD: "password" },
  useCredentialModalState: () => ({
    setIsOpen: vi.fn(),
    setType: vi.fn(),
  }),
}));
vi.mock("./useLoginGoalAutoFill", () => ({
  useLoginGoalAutoFill: () => undefined,
}));

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = () => {};
});

afterAll(() => {
  vi.unstubAllGlobals();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.credentialDetail.data = undefined;
  mocks.credentialDetail.error = null;
  mocks.credentialDetail.isError = false;
  mocks.credentialDetail.isPending = false;
  mocks.credentialDetailsById.clear();
  mocks.onePasswordItems.data = undefined;
  mocks.onePasswordItems.isLoading = false;
  mocks.onePasswordItems.isError = false;
  mocks.searchValues.length = 0;
  mocks.nodes = [];
  mocks.updateNodeData.mockImplementation(
    (nodeId: string, updates: Partial<WorkflowStartNodeData>) => {
      mocks.nodes = mocks.nodes.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, ...updates } }
          : node,
      );
    },
  );
  useWorkflowParametersStore.setState({ parameters: [] });
});

function credential(
  credentialId: string,
  name: string,
  testedUrl?: string,
): CredentialApiResponse {
  return {
    credential_id: credentialId,
    name,
    credential_type: "password",
    credential: {},
    tested_url: testedUrl,
  } as CredentialApiResponse;
}

function startNode(
  overrides: Partial<WorkflowStartNodeData>,
): (typeof mocks.nodes)[number] {
  return {
    id: "start",
    type: "start",
    data: {
      withWorkflowSettings: true,
      webhookCallbackUrl: "",
      proxyLocation: "RESIDENTIAL",
      persistBrowserSession: false,
      reuseBrowserSession: false,
      pinSavedSessionIp: false,
      browserProfileId: null,
      browserProfileKey: null,
      model: null,
      maxScreenshotScrolls: null,
      maxElapsedTimeMinutes: null,
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
      editable: true,
      runWith: "agent",
      codeVersion: null,
      scriptCacheKey: null,
      aiFallback: true,
      enableSelfHealing: false,
      maskSecrets: false,
      runSequentially: true,
      sequentialKey: null,
      finallyBlockLabel: null,
      workflowSystemPrompt: null,
      errorCodeMapping: null,
      label: "__start_block__",
      showCode: false,
      ...overrides,
    },
  };
}

function cloudTree(children: ReactNode, fallbackRetryEnabled = true) {
  return (
    <CloudContext.Provider value={true}>
      <FeatureFlagContext.Provider
        value={(flag) =>
          flag === CREDENTIAL_FALLBACK_RETRY_FLAG
            ? fallbackRetryEnabled
            : undefined
        }
      >
        {children}
      </FeatureFlagContext.Provider>
    </CloudContext.Provider>
  );
}

function renderInCloud(children: ReactNode, fallbackRetryEnabled = true) {
  return render(cloudTree(children, fallbackRetryEnabled));
}

describe("LoginBlockCredentialSelector", () => {
  it("resolves an out-of-page selected credential without marking it missing", async () => {
    mocks.credentialDetail.data = credential("cred_test_1", "Prod Login");
    mocks.useCredentialsQuery.mockReturnValue({
      data: [],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_test_1",
        },
      ],
    });

    renderInCloud(
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />,
    );

    expect(await screen.findByText("Prod Login")).toBeTruthy();
    expect(screen.queryByText("Credential not found")).toBeNull();
  });

  it("uses the searched credential record for parameter creation and URL autofill", async () => {
    const searchedCredential = credential(
      "old-credential",
      "Archived Login",
      "https://example.invalid/login",
    );
    mocks.useCredentialsQuery.mockImplementation(
      (props: { search?: string }) => {
        mocks.searchValues.push(props.search);
        return {
          data: props.search === "Archived" ? [searchedCredential] : [],
          isFetching: false,
          isLoading: false,
        };
      },
    );
    const onChange = vi.fn();
    const onUrlAutoFill = vi.fn();

    renderInCloud(
      <LoginBlockCredentialSelector
        nodeId="login-node"
        value=""
        onChange={onChange}
        onUrlAutoFill={onUrlAutoFill}
      />,
    );

    fireEvent.click(
      screen.getByRole("combobox", { name: "Select a credential" }),
    );
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "Archived" },
    });

    await waitFor(() => expect(mocks.searchValues).toContain("Archived"));
    fireEvent.click(await screen.findByText("Archived Login"));

    expect(onChange).toHaveBeenCalledWith("credentials");
    expect(onUrlAutoFill).toHaveBeenCalledWith("https://example.invalid/login");
    expect(useWorkflowParametersStore.getState().parameters).toContainEqual({
      key: "credentials",
      parameterType: "credential",
      credentialId: "old-credential",
    });
  });

  it("resolves persisted rotation credential names through detail queries", async () => {
    mocks.credentialDetailsById.set(
      "cred_test_1",
      credential("cred_test_1", "Primary Login"),
    );
    mocks.credentialDetailsById.set(
      "cred_test_2",
      credential("cred_test_2", "Backup Login"),
    );
    mocks.useCredentialsQuery.mockReturnValue({
      data: [],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_test_1",
          credentialIds: ["cred_test_1", "cred_test_2"],
          selectionStrategy: "round_robin",
        },
      ],
    });

    renderInCloud(
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />,
    );

    expect(await screen.findByText("Primary Login")).toBeTruthy();
    expect(screen.getByText("Backup Login")).toBeTruthy();
  });

  it("does not mark a credential missing for a non-404 detail error", () => {
    mocks.credentialDetail.error = { status: 500 };
    mocks.credentialDetail.isError = true;
    mocks.useCredentialsQuery.mockReturnValue({
      data: [],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_test_1",
        },
      ],
    });

    renderInCloud(
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />,
    );

    expect(screen.getByText("Couldn't load credential.")).toBeTruthy();
    expect(screen.queryByText("Credential not found")).toBeNull();
    expect(screen.getByRole("combobox").className).not.toContain(
      "border-red-500",
    );
  });
});

describe("LoginBlockCredentialSelector fallback picker", () => {
  beforeEach(() => {
    mocks.useCredentialsQuery.mockReturnValue({
      data: [
        credential("cred_primary", "primary_credential"),
        credential("cred_backup_1", "backup_credential_1"),
      ],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "portal_credential",
          parameterType: "credential",
          credentialId: "cred_primary",
        },
      ],
    });
  });

  function renderSelector() {
    return renderInCloud(
      <LoginBlockCredentialSelector
        nodeId="node-1"
        value="portal_credential"
      />,
    );
  }

  async function openFallbackPicker() {
    renderSelector();
    fireEvent.click(screen.getByText("Add fallback credentials"));
    const trigger = await screen.findByRole("button", {
      name: /add fallback credentials/i,
    });
    fireEvent.click(trigger);
    await screen.findByPlaceholderText("Search credentials...");
  }

  it("hides the fallback editor when CREDENTIAL_FALLBACK_RETRY is disabled", () => {
    renderInCloud(
      <LoginBlockCredentialSelector
        nodeId="node-1"
        value="portal_credential"
      />,
      false,
    );

    // Orgs outside the retry rollout must not be able to configure a fallback that never runs.
    expect(screen.queryByText("Add fallback credentials")).toBeNull();
  });

  it("hides fallback controls when credential rotation is configured", () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "portal_credential",
          parameterType: "credential",
          credentialId: "cred_primary",
          credentialIds: ["cred_primary", "cred_backup_1"],
        },
      ],
    });

    renderSelector();

    expect(screen.queryByText("Fallback credentials")).toBeNull();
    expect(screen.queryByText("Add fallback credentials")).toBeNull();
    expect(
      screen.getByText(
        "Fallback credentials can't be combined with credential rotation.",
      ),
    ).toBeTruthy();
  });

  it("hides credential rotation when fallback credentials are configured", () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "portal_credential",
          parameterType: "credential",
          credentialId: "cred_primary",
          fallbackCredentialIds: ["cred_backup_1"],
        },
      ],
    });

    renderSelector();

    expect(
      screen.queryByText("Rotate between multiple credentials"),
    ).toBeNull();
    expect(
      screen.getByText(
        "Credential rotation can't be combined with fallback credentials.",
      ),
    ).toBeTruthy();
  });

  it("Escape closes only the picker and does not reach window listeners", async () => {
    await openFallbackPicker();

    const windowEscapeSpy = vi.fn();
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        windowEscapeSpy();
      }
    };
    window.addEventListener("keydown", listener);
    try {
      fireEvent.keyDown(screen.getByPlaceholderText("Search credentials..."), {
        key: "Escape",
      });

      await waitFor(() => {
        expect(
          screen.queryByPlaceholderText("Search credentials..."),
        ).toBeNull();
      });
      expect(windowEscapeSpy).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", listener);
    }
  });

  it("selecting a fallback option writes it to the parameter store", async () => {
    await openFallbackPicker();

    fireEvent.click(await screen.findByText("backup_credential_1"));

    await waitFor(() => {
      const parameter = useWorkflowParametersStore
        .getState()
        .parameters.find((p) => p.key === "portal_credential");
      expect(parameter).toMatchObject({
        fallbackCredentialIds: ["cred_backup_1"],
      });
    });
  });

  it("the picker popover is modal so outside clicks cannot hit the canvas", async () => {
    await openFallbackPicker();

    await waitFor(() => {
      expect(document.body.style.pointerEvents).toBe("none");
    });
  });
});

describe("LoginBlockCredentialSelector browser session reuse", () => {
  beforeEach(() => {
    mocks.useCredentialsQuery.mockReturnValue({
      data: [
        credential("cred_primary", "primary_credential"),
        credential("cred_secondary", "secondary_credential"),
      ],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "credential",
          credentialId: "cred_primary",
          credentialIds: ["cred_primary", "cred_secondary"],
          selectionStrategy: "round_robin",
        },
      ],
    });
  });

  it("applies enable state before disabling and keeps persistence sticky", () => {
    mocks.nodes = [
      startNode({
        persistBrowserSession: false,
        reuseBrowserSession: false,
      }),
    ];
    const selector = (
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />
    );
    const view = renderInCloud(selector);
    const getToggle = () => {
      const label = screen.getByText("Reuse browser sessions");
      const toggle = label.parentElement?.parentElement?.querySelector(
        'button[role="switch"]',
      );
      expect(toggle).not.toBeNull();
      return toggle!;
    };

    fireEvent.click(getToggle());

    expect(mocks.updateNodeData).toHaveBeenLastCalledWith("start", {
      reuseBrowserSession: true,
      persistBrowserSession: true,
    });
    expect(mocks.nodes[0]?.data).toMatchObject({
      reuseBrowserSession: true,
      persistBrowserSession: true,
    });

    view.rerender(cloudTree(selector));
    expect(getToggle().getAttribute("data-state")).toBe("checked");
    fireEvent.click(getToggle());

    expect(mocks.updateNodeData).toHaveBeenLastCalledWith("start", {
      reuseBrowserSession: false,
      persistBrowserSession: true,
    });
    expect(mocks.nodes[0]?.data).toMatchObject({
      reuseBrowserSession: false,
      persistBrowserSession: true,
    });

    view.rerender(cloudTree(selector));
    expect(getToggle().getAttribute("data-state")).toBe("unchecked");
  });
});

describe("LoginBlockCredentialSelector 1Password items", () => {
  const loginItem: OnePasswordItemApiResponse = {
    item_id: "op_item_1",
    title: "Portal Login",
    vault_id: "op_vault_1",
    vault_name: "Engineering",
    category: "LOGIN",
    url: "https://portal.example.invalid/signin",
  };
  const cardItem: OnePasswordItemApiResponse = {
    item_id: "op_item_2",
    title: "Corporate Card",
    vault_id: "op_vault_1",
    vault_name: "Engineering",
    category: "CREDIT_CARD",
    url: null,
  };

  beforeEach(() => {
    mocks.useCredentialsQuery.mockReturnValue({
      data: [],
      isFetching: false,
      isLoading: false,
    });
    mocks.onePasswordItems.data = {
      configured: true,
      items: [loginItem, cardItem],
    };
  });

  it("binds a picked 1Password item to an onepassword parameter and autofills the URL", async () => {
    const onChange = vi.fn();
    const onUrlAutoFill = vi.fn();

    renderInCloud(
      <LoginBlockCredentialSelector
        nodeId="login-node"
        value=""
        onChange={onChange}
        onUrlAutoFill={onUrlAutoFill}
      />,
    );

    fireEvent.click(
      screen.getByRole("combobox", { name: "Select a credential" }),
    );
    // Only login-category items are offered for a login block.
    expect(screen.queryByText("Corporate Card")).toBeNull();
    fireEvent.click(await screen.findByText("Portal Login"));

    expect(onChange).toHaveBeenCalledWith("credentials");
    expect(onUrlAutoFill).toHaveBeenCalledWith(
      "https://portal.example.invalid/signin",
    );
    expect(useWorkflowParametersStore.getState().parameters).toContainEqual({
      key: "credentials",
      parameterType: "onepassword",
      vaultId: "op_vault_1",
      itemId: "op_item_1",
    });
  });

  it("renders a saved 1Password selection as the item, not the raw parameter key", async () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "onepassword",
          vaultId: "op_vault_1",
          itemId: "op_item_1",
        },
      ],
    });

    renderInCloud(
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />,
    );

    expect(await screen.findByText("Portal Login")).toBeTruthy();
    expect(screen.queryByText("Select a credential")).toBeNull();
  });

  it("drops the auto-generated 1Password wrapper when a Skyvern credential replaces it", async () => {
    mocks.useCredentialsQuery.mockReturnValue({
      data: [credential("cred_test_1", "Skyvern Login")],
      isFetching: false,
      isLoading: false,
    });
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "credentials",
          parameterType: "onepassword",
          vaultId: "op_vault_1",
          itemId: "op_item_1",
        },
      ],
    });

    renderInCloud(
      <LoginBlockCredentialSelector nodeId="login-node" value="credentials" />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByText("Skyvern Login"));

    // A stale onepassword wrapper would keep resolving a secret the block no
    // longer uses, and would collide with the next auto-generated key.
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      {
        key: "credentials",
        parameterType: "credential",
        credentialId: "cred_test_1",
      },
    ]);
  });
});
