import posthog from "posthog-js";

type ActivationSurface = "run_parameters" | "editor";

function capture(event: string, properties: Record<string, unknown>): void {
  try {
    posthog.capture(event, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

function credentialSetupShown(
  surface: ActivationSurface,
  blockCount: number,
): void {
  capture("onboarding.credential_setup_shown", {
    surface,
    block_count: blockCount,
  });
}

function credentialSetupCtaClicked(
  surface: ActivationSurface,
  blockCount: number,
): void {
  capture("onboarding.credential_setup_cta_clicked", {
    surface,
    block_count: blockCount,
  });
}

export const CredentialSetupTelemetry = {
  credentialSetupShown,
  credentialSetupCtaClicked,
} as const;

export type { ActivationSurface };
