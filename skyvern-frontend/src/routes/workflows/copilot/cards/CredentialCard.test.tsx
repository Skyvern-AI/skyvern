import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CREDENTIAL_REQUIRED_FRAME_BY_REASON,
  CREDENTIAL_REQUIRED_FRAME_MINIMAL,
  CREDENTIAL_REQUIRED_FRAME_NO_MESSAGE,
  RESOLVED_OUTCOME_CONNECTED,
  RESOLVED_OUTCOME_CONNECTED_UNNAMED,
  RESOLVED_OUTCOME_SKIPPED,
  RESOLVED_OUTCOME_TIMEOUT,
  buildCredentialRequiredFrame,
} from "./CredentialCard.fixtures";
import {
  CREDENTIAL_WHY_LINE_BY_REASON,
  CredentialCard,
  type CredentialPauseHistorical,
  type CredentialRequiredReason,
} from "./CredentialCard";

const { getClientMock, credsData, credsFail } = vi.hoisted(() => {
  const data = {
    current: [] as Array<{
      credential_id: string;
      name: string;
      credential_type?: string;
      credential?: { username: string };
    }>,
  };
  const fail = { current: false };
  const get = vi.fn((path: string) =>
    path === "/credentials"
      ? fail.current
        ? Promise.reject(new Error("network"))
        : Promise.resolve({ data: data.current })
      : Promise.resolve({ data: {} }),
  );
  return {
    getClientMock: vi.fn(() => Promise.resolve({ get })),
    credsData: data,
    credsFail: fail,
  };
});

vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

// Radix Popover + cmdk misbehave in jsdom (portals, pointer capture); stub them to plain wrappers so
// the picker's items render inline and are directly clickable, mirroring the Select mock below.
vi.mock("@/components/ui/popover", () => ({
  Popover: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  PopoverTrigger: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  PopoverContent: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/ui/command", () => ({
  Command: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  CommandInput: ({ placeholder }: { placeholder?: string }) => (
    <input placeholder={placeholder} />
  ),
  CommandList: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  CommandEmpty: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  CommandGroup: ({
    children,
    heading,
  }: {
    children?: ReactNode;
    heading?: string;
  }) => (
    <div>
      {heading ? <div>{heading}</div> : null}
      {children}
    </div>
  ),
  CommandItem: ({
    children,
    onSelect,
  }: {
    children?: ReactNode;
    onSelect?: () => void;
    value?: string;
  }) => (
    <button type="button" onClick={() => onSelect?.()}>
      {children}
    </button>
  ),
}));

afterEach(() => {
  cleanup();
  credsData.current = [];
  credsFail.current = false;
  vi.clearAllMocks();
});

describe("CredentialCard content", () => {
  it("renders the site parsed from login_page_urls in the headline", () => {
    const frame = buildCredentialRequiredFrame({
      login_page_urls: ["https://news.ycombinator.com/login?goto=news"],
    });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Copilot needs to sign in to news.ycombinator.com"),
    ).toBeTruthy();
  });

  it("falls back to a generic site label when login_page_urls is empty", () => {
    const frame = buildCredentialRequiredFrame({ login_page_urls: [] });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Copilot needs to sign in to the site"),
    ).toBeTruthy();
  });

  it("falls back to the raw string when login_page_urls[0] isn't a parseable URL", () => {
    const frame = buildCredentialRequiredFrame({
      login_page_urls: ["not a valid url"],
    });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Copilot needs to sign in to not a valid url"),
    ).toBeTruthy();
  });

  it("renders the frame message as lead prose", () => {
    const frame = buildCredentialRequiredFrame({
      message: "Custom ask text for this turn.",
    });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Custom ask text for this turn.")).toBeTruthy();
  });

  it("renders no lead paragraph when the frame has no message (today's credentialPrompt shape)", () => {
    const { container } = render(
      <CredentialCard
        frame={CREDENTIAL_REQUIRED_FRAME_NO_MESSAGE}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(container.querySelector("p.text-sm")).toBeNull();
    expect(
      screen.getByText("Copilot needs to sign in to news.ycombinator.com"),
    ).toBeTruthy();
  });

  it("renders from a minimal frame carrying only type and reason", () => {
    const onConnect = vi.fn();
    render(
      <CredentialCard
        frame={CREDENTIAL_REQUIRED_FRAME_MINIMAL}
        mode="terminal"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Copilot needs to sign in to the site"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        CREDENTIAL_WHY_LINE_BY_REASON.workflow_credential_inputs_unbound,
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect credential" }));
    expect(onConnect).toHaveBeenCalledWith(undefined);
  });

  it.each(
    Object.keys(CREDENTIAL_WHY_LINE_BY_REASON) as CredentialRequiredReason[],
  )("renders the reason-mapped why-line for %s", (reason) => {
    const frame = CREDENTIAL_REQUIRED_FRAME_BY_REASON[reason];
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText(CREDENTIAL_WHY_LINE_BY_REASON[reason]),
    ).toBeTruthy();
  });

  it("falls back to the generic sign-in why-line for an out-of-union reason value", () => {
    // Simulates a backend reason token shipped ahead of a frontend update —
    // the type cast is the point of the test.
    const frame = buildCredentialRequiredFrame({
      reason: "future_unknown_reason" as CredentialRequiredReason,
    });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText(
        CREDENTIAL_WHY_LINE_BY_REASON.workflow_credential_inputs_unbound,
      ),
    ).toBeTruthy();
  });
});

describe("CredentialCard callbacks", () => {
  it("calls onConnect(undefined) when the primary CTA is clicked", () => {
    const onConnect = vi.fn();
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Connect credential" }));
    expect(onConnect).toHaveBeenCalledWith(undefined);
  });

  it("calls onSkip when the dismiss button is clicked", () => {
    const onSkip = vi.fn();
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={onSkip}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});

describe("CredentialCard terminal org-credential picker", () => {
  it("fetches the org credentials and renders them as a picker, in API order", async () => {
    credsData.current = [
      { credential_id: "cred_new", name: "Newest" },
      { credential_id: "cred_old", name: "Oldest" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    // Most-recent-first is the API's order (created_at desc); the card renders it as-is.
    const newest = await screen.findByRole("button", { name: "Newest" });
    const oldest = screen.getByRole("button", { name: "Oldest" });
    expect(
      newest.compareDocumentPosition(oldest) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(getClientMock).toHaveBeenCalled();
  });

  it("sends the credential id and name when a stored credential is picked", async () => {
    const onConnect = vi.fn();
    credsData.current = [{ credential_id: "cred_hn", name: "HN login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "HN login" }));
    expect(onConnect).toHaveBeenCalledWith("cred_hn", "HN login");
  });

  it("degrades to the Connect-credential CTA only when the org has no credentials", async () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() => expect(getClientMock).toHaveBeenCalled());
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("degrades to the CTA when the fetch fails, without caching an empty list", async () => {
    credsFail.current = true;
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() => expect(getClientMock).toHaveBeenCalled());
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("does not fetch for a terminal receipt (resolved outcome)", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(getClientMock).not.toHaveBeenCalled();
  });

  it("labels each row with its username so identical names stay distinguishable", async () => {
    credsData.current = [
      {
        credential_id: "cred_1",
        name: "prod",
        credential_type: "password",
        credential: { username: "prod-us@example.com" },
      },
      {
        credential_id: "cred_2",
        name: "prod",
        credential_type: "password",
        credential: { username: "prod-eu@example.com" },
      },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("prod-us@example.com")).toBeTruthy();
    expect(screen.getByText("prod-eu@example.com")).toBeTruthy();
  });

  it("re-fetches the list when reloadKey changes so a just-created credential appears", async () => {
    credsData.current = [{ credential_id: "cred_old", name: "Old login" }];
    const { rerender } = render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        reloadKey={0}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Old login")).toBeTruthy();
    // The parent bumps reloadKey after a create; the one-shot fetch must re-run and surface the new one.
    credsData.current = [
      { credential_id: "cred_old", name: "Old login" },
      { credential_id: "cred_new", name: "New login" },
    ];
    rerender(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        reloadKey={1}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("New login")).toBeTruthy();
  });

  it("pins the frame's credential_refs under a Suggested group, full list still complete", async () => {
    credsData.current = [
      { credential_id: "cred_sug", name: "Suggested login" },
      { credential_id: "cred_a", name: "Other A" },
      { credential_id: "cred_b", name: "Other B" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame({ credential_refs: ["cred_sug"] })}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Suggested")).toBeTruthy();
    expect(screen.getByText("All credentials")).toBeTruthy();
    // Every credential is still present (the suggestion is pinned, not a filter).
    expect(
      screen.getByRole("button", { name: "Suggested login" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Other A" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Other B" })).toBeTruthy();
  });

  it("sends the suggested credential's id when it is picked", async () => {
    const onConnect = vi.fn();
    credsData.current = [
      { credential_id: "cred_sug", name: "Suggested login" },
      { credential_id: "cred_a", name: "Other A" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame({ credential_refs: ["cred_sug"] })}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Suggested login" }),
    );
    expect(onConnect).toHaveBeenCalledWith("cred_sug", "Suggested login");
  });

  it("renders a plain list with no Suggested group when the frame has no credential_refs", async () => {
    credsData.current = [
      { credential_id: "cred_a", name: "Login A" },
      { credential_id: "cred_b", name: "Login B" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame({ credential_refs: [] })}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByRole("button", { name: "Login A" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Login B" })).toBeTruthy();
    expect(screen.queryByText("Suggested")).toBeNull();
    expect(screen.queryByText("All credentials")).toBeNull();
  });

  it("does not submit a pick after the inline pause has expired", async () => {
    const onConnect = vi.fn();
    credsData.current = [{ credential_id: "cred_a", name: "Login A" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame({
          expires_at: new Date(Date.now() - 1000).toISOString(),
        })}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    // The row still renders (the always-open popover mock ignores `open`), but the expiry guard
    // blocks the pick so no already-rejected resume token is submitted.
    fireEvent.click(await screen.findByRole("button", { name: "Login A" }));
    expect(onConnect).not.toHaveBeenCalled();
  });
});

describe("CredentialCard keyboard reachability", () => {
  // jsdom doesn't move focus on a synthetic Tab keypress the way a real
  // browser does, so this can't honestly simulate Tab traversal — that was
  // verified with real CDP-driven input in DESIGN-OUT. What this CAN pin
  // honestly: every interactive control is a legitimate, individually
  // focusable target (no accidental tabIndex=-1 / non-focusable element),
  // and a disabled control is correctly excluded.
  it("lets every enabled control receive focus individually", async () => {
    credsData.current = [{ credential_id: "cred_hn", name: "HN login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const picker = await screen.findByRole("combobox");
    const skip = screen.getByRole("button", { name: "Skip for now" });
    const connect = screen.getByRole("button", { name: "Connect credential" });

    for (const el of [skip, connect, picker]) {
      el.focus();
      expect(document.activeElement).toBe(el);
    }
  });

  it("excludes a disabled control from receiving focus", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() - 5_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const connect = screen.getByRole("button", { name: "Connect credential" });
    connect.focus();
    expect(document.activeElement).not.toBe(connect);
  });
});

describe("CredentialCard countdown (inline-pause vs terminal)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("never renders a countdown in terminal mode, even with a future expiry", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.queryByText(/^\d+:\d{2}$/)).toBeNull();
    act(() => {
      vi.advanceTimersByTime(600_000);
    });
    expect(screen.queryByText("Timed out")).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "Connect credential" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("ticks a countdown down in inline-pause mode", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("5:00")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText("4:00")).toBeTruthy();
  });

  it("keeps the visible per-second countdown out of the aria-live region", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const visible = screen.getByText("5:00");
    expect(visible.getAttribute("aria-live")).toBeNull();
    // Hidden from assistive tech so it doesn't get read twice alongside the
    // sr-only announcement below.
    expect(visible.getAttribute("aria-hidden")).toBe("true");
  });

  it("announces the countdown sparsely (once a minute, not every second)", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 330_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const announcement = screen.getByText("5 minutes left to connect");
    expect(announcement.getAttribute("aria-live")).toBe("polite");

    // Ticking within the same minute must not change the announced text —
    // that's what would spam a screen reader every second. 330s -> 310s is
    // still inside the "5 minutes" bucket (floor(310/60) === 5).
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByText("5 minutes left to connect")).toBeTruthy();

    // Crossing the 300s/5:00 boundary is the only thing allowed to change it.
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByText("4 minutes left to connect")).toBeTruthy();
  });

  it("keeps the visible countdown and the sr-only announcement in agreement at the minute boundary", () => {
    // Regression pin: formatCountdown and formatCountdownAnnouncement used
    // to disagree right at a whole-minute mark (visible "1:00" vs announced
    // "less than a minute").
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("1:00")).toBeTruthy();
    expect(screen.getByText("1 minute left to connect")).toBeTruthy();
    expect(screen.queryByText("Less than a minute left to connect")).toBeNull();
  });

  it("disables every control once the countdown expires, and disabled controls stay inert", () => {
    const onConnect = vi.fn();
    const onSkip = vi.fn();
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={onSkip}
      />,
    );
    act(() => {
      vi.advanceTimersByTime(300_000);
    });
    expect(screen.getAllByText("Timed out").length).toBeGreaterThan(0);

    const connectButton = screen.getByRole("button", {
      name: "Connect credential",
    });
    const skipButton = screen.getByRole("button", { name: "Skip for now" });
    expect(connectButton.hasAttribute("disabled")).toBe(true);
    expect(skipButton.hasAttribute("disabled")).toBe(true);

    fireEvent.click(connectButton);
    fireEvent.click(skipButton);
    expect(onConnect).not.toHaveBeenCalled();
    expect(onSkip).not.toHaveBeenCalled();
  });

  it("stops the countdown interval once expired instead of ticking forever", () => {
    const frame = buildCredentialRequiredFrame({
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    act(() => {
      vi.advanceTimersByTime(300_000);
    });
    expect(screen.getAllByText("Timed out").length).toBeGreaterThan(0);
    // The interval is the only timer this hook schedules; once it self-clears
    // on expiry, nothing should remain pending.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("treats an unparseable expires_at as already expired instead of showing NaN:NaN", () => {
    const onConnect = vi.fn();
    const frame = buildCredentialRequiredFrame({ expires_at: "not-a-date" });
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getAllByText("Timed out").length).toBeGreaterThan(0);
    expect(screen.queryByText(/NaN/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Connect credential" }));
    expect(onConnect).not.toHaveBeenCalled();
  });
});

describe("CredentialCard terminal next-step hint", () => {
  it("shows the continue hint in terminal mode", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Connect a credential and I'll continue."),
    ).toBeTruthy();
  });

  it("omits the continue hint in inline-pause mode", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.queryByText("Connect a credential and I'll continue."),
    ).toBeNull();
  });
});

describe("CredentialCard historical (resolvedOutcome) rendering", () => {
  it("renders a fallback instead of crashing on an out-of-union outcome value", () => {
    // Simulates untyped network data reaching a compile-time-exhaustive
    // switch — the type cast is the point of the test.
    const outOfBandOutcome = {
      outcome: "archived",
    } as unknown as CredentialPauseHistorical;
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={outOfBandOutcome}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Credential status unavailable")).toBeTruthy();
  });

  it("renders a named receipt for a connected outcome and fires no callbacks on mount", () => {
    const onConnect = vi.fn();
    const onSkip = vi.fn();
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED}
        onConnect={onConnect}
        onSkip={onSkip}
      />,
    );
    expect(screen.getByText("Credential 'HN login' added")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
    expect(onConnect).not.toHaveBeenCalled();
    expect(onSkip).not.toHaveBeenCalled();
  });

  it("renders a 'Continuing' receipt when a terminal connect auto-continued", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED}
        continued
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Continuing with 'HN login'…")).toBeTruthy();
    expect(screen.queryByText(/added/)).toBeNull();
  });

  it("uses unnamed 'Continuing…' copy when the continued id has no match", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED_UNNAMED}
        continued
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Continuing…")).toBeTruthy();
  });

  it("falls back to unnamed receipt copy when the connected id has no matching credential", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED_UNNAMED}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Credential added")).toBeTruthy();
  });

  it("renders the muted skip row for a skipped outcome", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        resolvedOutcome={RESOLVED_OUTCOME_SKIPPED}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText(
        "Credential setup skipped — test run may stop at the login step",
      ),
    ).toBeTruthy();
  });

  it("renders the muted timeout row for a timeout outcome", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        resolvedOutcome={RESOLVED_OUTCOME_TIMEOUT}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText(
        "Credential request timed out — test run may stop at the login step",
      ),
    ).toBeTruthy();
  });
});
