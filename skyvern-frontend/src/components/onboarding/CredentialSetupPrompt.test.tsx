// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/util/onboarding/credentialSetupTelemetry", () => ({
  CredentialSetupTelemetry: {
    credentialSetupShown: vi.fn(),
    credentialSetupCtaClicked: vi.fn(),
  },
}));

const { studioState } = vi.hoisted(() => ({ studioState: { enabled: true } }));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => studioState.enabled,
}));

import { CredentialSetupTelemetry } from "@/util/onboarding/credentialSetupTelemetry";
import { CredentialSetupPrompt } from "./CredentialSetupPrompt";

function renderPrompt(blocks: Array<{ label: string }>) {
  return render(
    <MemoryRouter>
      <CredentialSetupPrompt
        workflowPermanentId="wpid_123"
        blocksMissingCredentials={blocks}
      />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  studioState.enabled = true;
});

describe("CredentialSetupPrompt", () => {
  it("lists each login block missing a credential and routes the CTA into the editor", () => {
    renderPrompt([{ label: "Login" }, { label: "Member sign in" }]);

    // getByText throws when absent, so these assert presence on their own.
    screen.getByText("Login");
    screen.getByText("Member sign in");
    expect(CredentialSetupTelemetry.credentialSetupShown).toHaveBeenCalledWith(
      "run_parameters",
      2,
    );

    const cta = screen.getByRole("link", {
      name: /set up credentials in the editor/i,
    });
    expect(cta.getAttribute("href")).toBe("/workflows/wpid_123/studio");
  });

  it("routes the CTA to /build when the studio preview is off", () => {
    studioState.enabled = false;
    renderPrompt([{ label: "Login" }]);

    const cta = screen.getByRole("link", {
      name: /set up credentials in the editor/i,
    });
    expect(cta.getAttribute("href")).toBe("/workflows/wpid_123/build");
  });

  it("fires credentialSetupShown once on mount with the block count", () => {
    renderPrompt([{ label: "Login" }]);

    expect(CredentialSetupTelemetry.credentialSetupShown).toHaveBeenCalledTimes(
      1,
    );
    expect(CredentialSetupTelemetry.credentialSetupShown).toHaveBeenCalledWith(
      "run_parameters",
      1,
    );
  });

  it("does not re-fire credentialSetupShown on re-render", () => {
    const { rerender } = renderPrompt([{ label: "Login" }]);

    rerender(
      <MemoryRouter>
        <CredentialSetupPrompt
          workflowPermanentId="wpid_123"
          blocksMissingCredentials={[{ label: "Login" }]}
        />
      </MemoryRouter>,
    );

    expect(CredentialSetupTelemetry.credentialSetupShown).toHaveBeenCalledTimes(
      1,
    );
  });

  it("fires credentialSetupCtaClicked when the CTA is clicked", () => {
    renderPrompt([{ label: "Login" }]);

    fireEvent.click(
      screen.getByRole("link", { name: /set up credentials in the editor/i }),
    );

    expect(
      CredentialSetupTelemetry.credentialSetupCtaClicked,
    ).toHaveBeenCalledWith("run_parameters", 1);
  });

  it("renders nothing and fires no telemetry when no blocks are missing credentials", () => {
    const { container } = renderPrompt([]);

    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      CredentialSetupTelemetry.credentialSetupShown,
    ).not.toHaveBeenCalled();
  });
});
