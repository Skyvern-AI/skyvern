// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ post: mockPost }),
}));

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));

vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: {
    flowCompleted: vi.fn(),
    modalCopilotClicked: vi.fn(),
    modalTemplateSelected: vi.fn(),
  },
}));

import { Dialog, DialogContent } from "@/components/ui/dialog";
import { CopilotCTAStep } from "./CopilotCTAStep";

function setup(onBusyChange?: (busy: boolean) => void) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Dialog defaultOpen>
          <DialogContent>
            <CopilotCTAStep
              selectedIntent="fill_forms"
              onBack={vi.fn()}
              onSkip={vi.fn()}
              onDismiss={vi.fn()}
              onBusyChange={onBusyChange}
            />
          </DialogContent>
        </Dialog>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidateSpy };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CopilotCTAStep", () => {
  it("invalidates userOnboarding after the copilot handoff creates a workflow", async () => {
    mockPost.mockResolvedValue({
      data: { workflow_permanent_id: "wpid_x" },
    });
    const { invalidateSpy } = setup();

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "fill out my form" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create with AI" }));

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["userOnboarding"],
      }),
    );
  });

  it("reports busy to the parent while the handoff is in flight", async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    const onBusyChange = vi.fn();
    setup(onBusyChange);

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "fill out my form" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create with AI" }));

    await waitFor(() => expect(onBusyChange).toHaveBeenCalledWith(true));
  });

  it("disables Skip while the copilot handoff is in flight", async () => {
    // Never resolves: keep the handoff mutation pending so we can assert the
    // Skip control is disabled (otherwise a skip mid-create still navigates).
    mockPost.mockReturnValue(new Promise(() => {}));
    setup();

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "fill out my form" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create with AI" }));

    await waitFor(() => {
      const skip = screen.getByRole("button", { name: "Skip" });
      expect((skip as HTMLButtonElement).disabled).toBe(true);
    });
  });
});
