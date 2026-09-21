import posthog from "posthog-js";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OnboardingTelemetry } from "./OnboardingTelemetry";

vi.mock("posthog-js", () => ({
  default: {
    capture: vi.fn(),
    register: vi.fn(),
    getGroups: vi.fn(() => ({ organization: "org_123" })),
  },
}));

const customDedupeProperty = ["$", "insert", "_id"].join("");
const captureResult = {
  uuid: "capture-id",
  event: "captured",
  properties: {},
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(posthog.capture).mockReturnValue(captureResult);
});

describe("OnboardingTelemetry", () => {
  it("captures layer-2 hint events", () => {
    OnboardingTelemetry.hintShown("editor", "add-another-block");
    OnboardingTelemetry.hintDismissed("runs", "run-recording");
    expect(posthog.capture).toHaveBeenNthCalledWith(
      1,
      "onboarding.hint_shown",
      {
        surface: "editor",
        hint_id: "add-another-block",
        layer: 2,
        organization_id: "org_123",
        step_id: "onboarding.hint_shown:v1",
      },
    );
    expect(posthog.capture).toHaveBeenNthCalledWith(
      2,
      "onboarding.hint_dismissed",
      {
        surface: "runs",
        hint_id: "run-recording",
        layer: 2,
        organization_id: "org_123",
        step_id: "onboarding.hint_dismissed:v1",
      },
    );
  });

  it("adds organization and versioned step identity without custom dedupe", () => {
    OnboardingTelemetry.stepCompleted("discover", "first_agent_created");

    expect(posthog.capture).toHaveBeenCalledWith("onboarding.step_completed", {
      surface: "discover",
      step: "first_agent_created",
      organization_id: "org_123",
      step_id: "first_agent_created:v1",
    });
    expect(posthog.capture).not.toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        [customDedupeProperty]: expect.anything(),
      }),
    );
  });

  it("reports whether PostHog accepted a questionnaire capture", () => {
    const input = {
      primaryIntent: "fill_forms",
      promptReason: "initial",
      organizationId: null,
    } as const;
    expect(OnboardingTelemetry.questionnaireShown(input)).toBe(true);

    vi.mocked(posthog.capture).mockReturnValueOnce(undefined);
    expect(OnboardingTelemetry.questionnaireShown(input)).toBe(false);

    vi.mocked(posthog.capture).mockImplementationOnce(() => {
      throw new Error("capture unavailable");
    });
    expect(OnboardingTelemetry.questionnaireShown(input)).toBe(false);
  });

  it("prefers the server-provided organization over the group lookup", () => {
    OnboardingTelemetry.questionnaireShown({
      primaryIntent: null,
      promptReason: "initial",
      organizationId: "org_from_server",
    });
    expect(posthog.getGroups).not.toHaveBeenCalled();
    expect(posthog.capture).toHaveBeenLastCalledWith(
      "onboarding_questionnaire_shown",
      expect.objectContaining({ organization_id: "org_from_server" }),
    );
  });

  it("captures the exact bounded questionnaire dictionaries", () => {
    const answers = {
      role: "developer",
      company_context: "startup",
      scale_intent: "exploring",
      referral_source: "search",
    } as const;
    OnboardingTelemetry.questionnaireShown({
      primaryIntent: null,
      promptReason: "initial",
      organizationId: null,
    });
    OnboardingTelemetry.questionnaireCompleted({
      responseId: "response",
      revision: 1,
      primaryIntent: "fill_forms",
      answers,
    });
    OnboardingTelemetry.questionnaireSkipped({
      responseId: "response",
      revision: 2,
      disposition: "skip",
      statusAfter: "skipped",
    });
    OnboardingTelemetry.questionnaireUpdated({
      responseId: "response",
      revision: 3,
      primaryIntent: "fill_forms",
      previousStatus: "deferred",
      answers,
    });

    expect(posthog.capture).toHaveBeenCalledTimes(4);
    expect(posthog.capture).toHaveBeenNthCalledWith(
      1,
      "onboarding_questionnaire_shown",
      {
        questionnaire_version: 1,
        "$feature/onboarding_questionnaire_v1": true,
        surface: "get_started_modal",
        primary_intent: null,
        prompt_reason: "initial",
        organization_id: "org_123",
        step_id: "onboarding_questionnaire_shown:v1",
      },
    );
    expect(posthog.capture).toHaveBeenNthCalledWith(
      2,
      "onboarding_questionnaire_completed",
      {
        questionnaire_version: 1,
        "$feature/onboarding_questionnaire_v1": true,
        surface: "get_started_modal",
        response_id: "response",
        revision: 1,
        primary_intent: "fill_forms",
        ...answers,
        organization_id: "org_123",
        step_id: "onboarding_questionnaire_completed:v1",
        project_owner_reported: false,
        project_owner_name_present: false,
        project_owner_email_present: false,
        project_owner_role_present: false,
      },
    );
    expect(posthog.capture).toHaveBeenNthCalledWith(
      3,
      "onboarding_questionnaire_skipped",
      {
        questionnaire_version: 1,
        "$feature/onboarding_questionnaire_v1": true,
        surface: "get_started_modal",
        response_id: "response",
        revision: 2,
        disposition: "skip",
        status_after: "skipped",
        organization_id: "org_123",
        step_id: "onboarding_questionnaire_skipped:v1",
      },
    );
    expect(posthog.capture).toHaveBeenNthCalledWith(
      4,
      "onboarding_questionnaire_updated",
      {
        questionnaire_version: 1,
        "$feature/onboarding_questionnaire_v1": true,
        surface: "get_started_modal",
        response_id: "response",
        revision: 3,
        primary_intent: "fill_forms",
        previous_status: "deferred",
        ...answers,
        organization_id: "org_123",
        step_id: "onboarding_questionnaire_updated:v1",
        project_owner_reported: false,
        project_owner_name_present: false,
        project_owner_email_present: false,
        project_owner_role_present: false,
      },
    );
    expect(JSON.stringify(vi.mocked(posthog.capture).mock.calls)).not.toContain(
      customDedupeProperty,
    );
  });

  it("projects only current-org presence and never owner PII", () => {
    const input = {
      responseId: "response",
      revision: 2,
      primaryIntent: "fill_forms" as const,
      organizationId: "o_a",
      previousStatus: "completed" as const,
      answers: {
        role: "developer",
        company_context: "startup",
        scale_intent: "exploring",
        referral_source: "search",
      } as const,
      projectOwner: {
        version: 1,
        organization_id: "o_a",
        reported_by_user_id: "user_a",
        source: "signup_user_reported",
        verification: "unverified",
        contact_permission: "not_granted",
        name: "Synthetic Owner",
        professional_email: "owner@example.com",
        role: "Synthetic sponsor role",
        reported_at: "2026-09-19T00:00:00Z",
        updated_at: "2026-09-19T00:00:00Z",
      } as const,
    };
    OnboardingTelemetry.questionnaireCompleted(input);
    OnboardingTelemetry.questionnaireUpdated(input);
    for (const [, properties] of vi.mocked(posthog.capture).mock.calls) {
      expect(properties).toEqual(
        expect.objectContaining({
          organization_id: "o_a",
          project_owner_reported: true,
          project_owner_name_present: true,
          project_owner_email_present: true,
          project_owner_role_present: true,
          project_owner_source: "signup_user_reported",
        }),
      );
    }
    OnboardingTelemetry.questionnaireUpdated({
      ...input,
      organizationId: "o_b",
    });
    expect(posthog.capture).toHaveBeenLastCalledWith(
      "onboarding_questionnaire_updated",
      expect.objectContaining({
        organization_id: "o_b",
        project_owner_reported: false,
        project_owner_email_present: false,
      }),
    );
    const sent = JSON.stringify(vi.mocked(posthog.capture).mock.calls);
    for (const value of [
      input.projectOwner.name,
      input.projectOwner.professional_email,
      input.projectOwner.role,
    ]) {
      expect(sent).not.toContain(value);
    }
    vi.mocked(posthog.capture).mockImplementationOnce(() => {
      throw new Error("offline");
    });
    expect(() =>
      OnboardingTelemetry.questionnaireCompleted(input),
    ).not.toThrow();
  });
});
