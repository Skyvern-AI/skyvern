// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OnboardingState } from "@/store/onboarding/types";

const { mockUpdateState, mockMutate, mockTelemetry, mutationState, flagState } =
  vi.hoisted(() => ({
    mockUpdateState: vi.fn(),
    mockMutate: vi.fn(),
    mockTelemetry: {
      registerVariant: vi.fn(),
      flowStarted: vi.fn(),
      modalOpened: vi.fn(),
      abVariantAssigned: vi.fn(),
      modalSkipped: vi.fn(),
      modalTemplateSelected: vi.fn(),
    },
    mutationState: { isPending: false },
    flagState: { variant: "template-first" },
  }));

const baseState: OnboardingState = {
  tour_completed_at: null,
  modal_dismissed_at: null,
  first_save_at: null,
  first_run_at: null,
  ab_variant: "template-first",
  user_intent: "fill_forms",
  seen_canvas: null,
  seen_node_adder: null,
  seen_sidebar: null,
  seen_save_run: null,
};

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => flagState.variant,
}));

vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingState: () => ({
    state: { ...baseState, ab_variant: flagState.variant },
    isLoading: false,
    updateState: mockUpdateState,
    isNewUser: true,
    abVariant: flagState.variant,
  }),
}));

vi.mock("./CopilotCTAStep", async () => {
  const { useEffect } = await import("react");
  return {
    CopilotCTAStep: (props: {
      onSkip: () => void;
      onBusyChange?: (busy: boolean) => void;
    }) => {
      useEffect(() => {
        props.onBusyChange?.(true);
      }, [props]);
      return (
        <button type="button" onClick={props.onSkip}>
          child-skip
        </button>
      );
    },
  };
});

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));

vi.mock("@/routes/workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: () => ({
    mutate: mockMutate,
    isPending: mutationState.isPending,
  }),
}));

vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: mockTelemetry,
}));

import { GetStartedModal } from "./GetStartedModal";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mutationState.isPending = false;
  flagState.variant = "template-first";
});

describe("GetStartedModal skip-while-creating guard", () => {
  it("blocks Skip while a template workflow creation is in flight", () => {
    mutationState.isPending = true;
    render(<GetStartedModal hasWorkflows={false} isLoading={false} />);
    const skip = screen.getByRole("button", { name: "Skip" });
    expect((skip as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(skip);
    expect(mockTelemetry.modalSkipped).not.toHaveBeenCalled();
    expect(mockUpdateState).not.toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });

  it("dismisses on Skip when no creation is pending", () => {
    mutationState.isPending = false;
    render(<GetStartedModal hasWorkflows={false} isLoading={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(mockTelemetry.modalSkipped).toHaveBeenCalledTimes(1);
    expect(mockUpdateState).toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });

  it("ignores skip and Escape-close while the copilot step reports busy", () => {
    flagState.variant = "copilot-first";
    render(<GetStartedModal hasWorkflows={false} isLoading={false} />);

    fireEvent.click(screen.getByRole("button", { name: "child-skip" }));
    fireEvent.keyDown(document, { key: "Escape" });

    expect(mockTelemetry.modalSkipped).not.toHaveBeenCalled();
    expect(mockUpdateState).not.toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });
});
