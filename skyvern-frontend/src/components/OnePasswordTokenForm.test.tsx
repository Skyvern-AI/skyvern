// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  token: null as { token: string; modified_at: string } | null,
  isLoading: false,
}));

vi.mock("@/hooks/useOnePasswordToken", () => ({
  useOnePasswordToken: () => ({
    onePasswordToken: mocks.token,
    isLoading: mocks.isLoading,
    createOrUpdateToken: vi.fn(),
    isUpdating: false,
    clearToken: vi.fn(),
    isClearing: false,
  }),
}));

import { OnePasswordTokenForm } from "./OnePasswordTokenForm";

const A_STEP_BODY = /Vault permissions/;

beforeEach(() => {
  mocks.token = null;
  mocks.isLoading = false;
});

afterEach(cleanup);

describe("OnePasswordTokenForm setup guide", () => {
  it("expands the steps for an org that has not connected 1Password yet", () => {
    render(<OnePasswordTokenForm />);

    expect(screen.getByText(A_STEP_BODY)).toBeTruthy();
  });

  it("collapses the steps once a token is configured", () => {
    mocks.token = { token: "ops_test", modified_at: "2026-08-20T00:00:00Z" };

    render(<OnePasswordTokenForm />);

    // Header stays available so a rotating admin can still reach the steps.
    expect(
      screen.getByText("How to create a service account token"),
    ).toBeTruthy();
    expect(screen.queryByText(A_STEP_BODY)).toBeNull();
  });

  it("waits for the token query before deciding, so a configured org never flashes the steps open", () => {
    // The query resolves after first paint. Reading it while still loading
    // latches the guide open for orgs that are already connected.
    mocks.isLoading = true;
    const view = render(<OnePasswordTokenForm />);
    expect(screen.queryByText(A_STEP_BODY)).toBeNull();

    mocks.isLoading = false;
    mocks.token = { token: "ops_test", modified_at: "2026-08-20T00:00:00Z" };
    view.rerender(<OnePasswordTokenForm />);

    expect(screen.queryByText(A_STEP_BODY)).toBeNull();
  });
});
