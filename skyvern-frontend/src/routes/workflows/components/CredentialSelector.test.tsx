import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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

import type { CredentialApiResponse } from "@/api/types";
import { CredentialSelector } from "./CredentialSelector";

const mocks = vi.hoisted(() => ({
  getClient: vi.fn(),
  onModalOpen: vi.fn(),
  setModalType: vi.fn(),
  useCredentialsQuery: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: mocks.getClient,
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/routes/workflows/editor/WorkflowScopeContext", () => ({
  useWorkflowScopeReadOnly: () => false,
}));
vi.mock("../hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: mocks.useCredentialsQuery,
}));
vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: () => null,
}));
vi.mock("@/routes/credentials/useCredentialModalState", () => ({
  CredentialModalTypes: { PASSWORD: "password" },
  useCredentialModalState: () => ({
    setIsOpen: mocks.onModalOpen,
    setType: mocks.setModalType,
  }),
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
});

function credential(credentialId: string, name: string): CredentialApiResponse {
  return {
    credential_id: credentialId,
    name,
    credential_type: "password",
    credential: {},
  } as CredentialApiResponse;
}

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  };
}

describe("CredentialSelector", () => {
  it("renders the selected credential name when it is absent from the first page", async () => {
    const selectedCredential = credential("cred_test_1", "Prod Login");
    mocks.useCredentialsQuery.mockReturnValue({
      data: [credential("cred_recent", "Recent Login")],
      isFetching: false,
      isLoading: false,
    });
    mocks.getClient.mockResolvedValue({
      get: vi.fn().mockResolvedValue({ data: selectedCredential }),
    });

    render(<CredentialSelector value="cred_test_1" onChange={() => {}} />, {
      wrapper: makeWrapper(),
    });

    expect(await screen.findByText("Prod Login")).toBeTruthy();
  });

  it("finds and selects an older credential through debounced server search", async () => {
    const oldCredential = credential("old-credential", "Archived Login");
    let latestSearch: string | undefined;
    mocks.useCredentialsQuery.mockImplementation(
      (props: { search?: string }) => {
        latestSearch = props.search;
        return {
          data: props.search === "Archived" ? [oldCredential] : [],
          isFetching: false,
          isLoading: false,
        };
      },
    );
    const onChange = vi.fn();

    render(<CredentialSelector value="" onChange={onChange} />, {
      wrapper: makeWrapper(),
    });

    fireEvent.click(
      screen.getByRole("combobox", { name: "Select a credential" }),
    );
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "Archived" },
    });

    await waitFor(() => expect(latestSearch).toBe("Archived"));
    fireEvent.click(await screen.findByText("Archived Login"));

    expect(onChange).toHaveBeenCalledWith("old-credential");
  });
});
