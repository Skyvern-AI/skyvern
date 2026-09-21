import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CREDENTIAL_REQUIRED_FRAME_BY_REASON,
  CREDENTIAL_REQUIRED_FRAME_MINIMAL,
  CREDENTIAL_REQUIRED_FRAME_NO_MESSAGE,
  CREDENTIAL_REQUIRED_FRAME_REDACTED_ASK,
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

const { getClientMock, credsData, credsFail, clientGet } = vi.hoisted(() => {
  const data = {
    current: [] as Array<{
      credential_id: string;
      name: string;
      credential_type?: string;
      credential?: { username: string };
    }>,
  };
  const fail = { current: false };
  // Stands in for the route's `search` param (case-insensitive across name and username) so a test
  // can prove the picker's box reaches the server rather than filtering the fetched page.
  const get = vi.fn(
    (path: string, config?: { params?: { search?: string } }) => {
      if (path.startsWith("/credentials/")) {
        if (fail.current) return Promise.reject(new Error("network"));
        const id = decodeURIComponent(path.slice("/credentials/".length));
        return Promise.resolve({
          data: data.current.find((c) => c.credential_id === id),
        });
      }
      if (path !== "/credentials") return Promise.resolve({ data: {} });
      if (fail.current) return Promise.reject(new Error("network"));
      const term = config?.params?.search?.toLowerCase();
      return Promise.resolve({
        data: term
          ? data.current.filter((credential) =>
              `${credential.name} ${credential.credential?.username ?? ""}`
                .toLowerCase()
                .includes(term),
            )
          : data.current,
      });
    },
  );
  return {
    getClientMock: vi.fn(() => Promise.resolve({ get })),
    credsData: data,
    credsFail: fail,
    clientGet: get,
  };
});

vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

// Radix Popover + cmdk misbehave in jsdom (portals, pointer capture); stub them to plain wrappers so
// the picker's items render inline and are directly clickable, mirroring the Select mock below.
vi.mock("@/components/ui/popover", () => ({
  // Exposes onOpenChange as a clickable close, since the real dismissal is a Radix pointer gesture
  // jsdom cannot perform. Nothing else about the card reads it.
  Popover: ({
    children,
    onOpenChange,
  }: {
    children?: ReactNode;
    onOpenChange?: (open: boolean) => void;
  }) => (
    <div>
      <button type="button" onClick={() => onOpenChange?.(false)}>
        close-popover
      </button>
      {children}
    </div>
  ),
  PopoverTrigger: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  PopoverContent: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/ui/command", () => ({
  Command: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  CommandInput: ({
    placeholder,
    value,
    onValueChange,
  }: {
    placeholder?: string;
    value?: string;
    onValueChange?: (value: string) => void;
  }) => (
    <input
      placeholder={placeholder}
      value={value}
      onChange={(event) => onValueChange?.(event.target.value)}
    />
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
  it.each([
    "https://news.ycombinator.com",
    "http://news.ycombinator.com",
    "https://news.ycombinator.com:8443",
  ])("renders the full origin %s in the headline", (origin) => {
    const frame = buildCredentialRequiredFrame({
      login_page_urls: [`${origin}/login?goto=news`],
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
      screen.getByText(`Copilot needs to sign in to ${origin}`),
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
      screen.getByText(
        "Copilot needs to sign in to https://news.ycombinator.com",
      ),
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

  it("says the list is loading while the fetch is in flight, CTA still usable", () => {
    credsData.current = [{ credential_id: "cred_a", name: "Login A" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain(
      "Loading saved logins",
    );
    expect(screen.queryByRole("button", { name: "Login A" })).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "Connect credential" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("leaves the announcement to a surrounding live region instead of nesting one", () => {
    credsData.current = [{ credential_id: "cred_a", name: "Login A" }];
    render(
      <div role="status" aria-live="polite" data-testid="chat-live-region">
        <CredentialCard
          frame={buildCredentialRequiredFrame()}
          mode="inline-pause"
          onConnect={vi.fn()}
          onSkip={vi.fn()}
        />
      </div>,
    );
    expect(screen.getByText(/Loading saved logins/)).toBeTruthy();
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status").dataset.testid).toBe("chat-live-region");
  });

  it("keeps its own live region for an inline pause restored outside the chat's", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsFail.current = true;
    // A pause restored after a reload renders beside, not inside, the chat's live region.
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const liveRegion = screen.getByRole("status");
    await waitFor(() =>
      expect(liveRegion.textContent).toBe("Couldn't load your saved logins."),
    );
    expect(screen.getByRole("status")).toBe(liveRegion);
    errSpy.mockRestore();
  });

  it("shows an empty org as empty — no error, no loading text, creation still offered", async () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe(""),
    );
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText(/Couldn't load your saved logins/)).toBeNull();
  });

  it("shows a failed fetch as failed, and Retry recovers to a usable picker", async () => {
    const onConnect = vi.fn();
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsFail.current = true;
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="terminal"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    // Captured before the fetch settles: a live region is only announced when it was already in the
    // tree when its text changed, so the failure must land in this same element.
    const liveRegion = screen.getByRole("status");
    expect(
      await screen.findByText(/Couldn't load your saved logins/, {
        selector: ":not(.sr-only)",
      }),
    ).toBeTruthy();
    expect(screen.getByRole("status")).toBe(liveRegion);
    expect(liveRegion.textContent).toBe("Couldn't load your saved logins.");
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    // The AxiosError-shaped rejection serializes credentials into console output otherwise.
    const logged = errSpy.mock.calls.find((call) =>
      String(call[0]).includes("Failed to load credentials"),
    );
    expect(typeof logged![1]).toBe("string");
    errSpy.mockRestore();

    credsFail.current = false;
    credsData.current = [
      { credential_id: "cred_recovered", name: "Recovered" },
    ];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.click(await screen.findByRole("button", { name: "Recovered" }));
    expect(onConnect).toHaveBeenCalledWith("cred_recovered", "Recovered");
    expect(screen.queryByText(/Couldn't load your saved logins/)).toBeNull();
  });

  it("disables Retry once the inline pause has expired", async () => {
    credsFail.current = true;
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame({
          expires_at: new Date(Date.now() - 1000).toISOString(),
        })}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(retry.hasAttribute("disabled")).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "Connect credential" })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("offers an explicitly listed credential even when the ask carries no candidate ids", async () => {
    const onConnect = vi.fn();
    credsData.current = [
      { credential_id: "cred_listed", name: "Listed login" },
    ];
    render(
      <CredentialCard
        frame={CREDENTIAL_REQUIRED_FRAME_REDACTED_ASK}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Listed login" }),
    );
    expect(onConnect).toHaveBeenCalledWith("cred_listed", "Listed login");
  });

  it("never picks a sole credential on the user's behalf", async () => {
    const onConnect = vi.fn();
    credsData.current = [{ credential_id: "cred_only", name: "Only login" }];
    render(
      <CredentialCard
        frame={CREDENTIAL_REQUIRED_FRAME_REDACTED_ASK}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    await screen.findByRole("button", { name: "Only login" });
    expect(onConnect).not.toHaveBeenCalled();
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

  it("keeps a loaded picker when a re-fetch fails instead of falling back to create-only", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsData.current = [{ credential_id: "cred_kept", name: "Kept login" }];
    const { rerender } = render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        reloadKey={0}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Kept login")).toBeTruthy();

    credsFail.current = true;
    rerender(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        reloadKey={1}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(
        errSpy.mock.calls.some((call) =>
          String(call[0]).includes("Failed to load credentials"),
        ),
      ).toBe(true),
    );
    expect(screen.getByText("Kept login")).toBeTruthy();
    expect(screen.queryByText(/Couldn't load your saved logins/)).toBeNull();
    errSpy.mockRestore();
  });

  it("searches the server rather than the fetched page, so a login past the page cap is reachable", async () => {
    credsData.current = [{ credential_id: "cred_page1", name: "On page one" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("On page one")).toBeTruthy();

    // Stands for a credential the org owns beyond the 100 the unpaginated fetch returns: it is
    // absent from the rendered page and only a server-side search can surface it.
    credsData.current = [
      { credential_id: "cred_beyond", name: "Beyond the cap" },
    ];
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "beyond" },
    });
    expect(await screen.findByText("Beyond the cap")).toBeTruthy();
    expect(clientGet).toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: expect.objectContaining({ search: "beyond" }),
      }),
    );
  });

  it("caps the search box at the route's limit, so the rows never answer a hidden prefix", async () => {
    credsData.current = [{ credential_id: "cred_a", name: "Acme login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Acme login")).toBeTruthy();

    // The route caps search at 200 characters, so an unclamped paste 422s into a failure whose
    // Retry re-sends the same rejected term and can never succeed. Clamping only the request would
    // instead leave the box showing 250 characters while the rows answered the first 200 — with
    // local filtering off, those rows read as matching the whole visible term.
    const box = screen.getByPlaceholderText(
      "Search credentials...",
    ) as HTMLInputElement;
    fireEvent.change(box, { target: { value: "z".repeat(250) } });
    expect(box.value).toHaveLength(200);
    await waitFor(() =>
      expect(
        clientGet.mock.calls.some(
          (call) =>
            (call[1] as { params?: { search?: string } })?.params?.search
              ?.length === 200,
        ),
      ).toBe(true),
    );
    expect(
      clientGet.mock.calls.every(
        (call) =>
          ((call[1] as { params?: { search?: string } })?.params?.search ?? "")
            .length <= 200,
      ),
    ).toBe(true);

    // The route counts code points, and an emoji is two UTF-16 units. Clamping by units would keep
    // half of it, which Axios cannot URL-encode, so the search would fail on every Retry.
    const atLimit = "a".repeat(199) + "😀";
    fireEvent.change(box, { target: { value: `${atLimit}b` } });
    expect(box.value).toBe(atLimit);
    expect(Array.from(box.value)).toHaveLength(200);
  });

  it("says when a full page of matches hides the rest instead of reading as the complete set", async () => {
    credsData.current = Array.from({ length: 100 }, (_unused, index) => ({
      credential_id: `cred_${index}`,
      name: `Login ${index}`,
    }));
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Login 0")).toBeTruthy();
    expect(screen.getByText(/Showing the first 100/)).toBeTruthy();

    credsData.current = credsData.current.slice(0, 99);
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "Login" },
    });
    await waitFor(() =>
      expect(screen.queryByText(/Showing the first 100/)).toBeNull(),
    );
  });

  it("drops a zero-match term when the popover closes, so the add-credential route comes back", async () => {
    credsData.current = [
      { credential_id: "cred_other", name: "Other login" },
      { credential_id: "cred_work", name: "Work login" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      await screen.findByRole("button", { name: "Other login" }),
    ).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "nothing-matches-this" },
    });
    expect(await screen.findByText("No credentials found.")).toBeTruthy();

    // A term left behind keeps the card in search mode over an empty result, which on this receipt
    // hides both the remaining logins and the only route to the add-credential modal and Retry.
    fireEvent.click(screen.getByRole("button", { name: "close-popover" }));
    expect(
      await screen.findByRole("button", { name: "Other login" }),
    ).toBeTruthy();
    expect(
      (screen.getByPlaceholderText("Search credentials...") as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("keeps the picker in place while a cleared search reloads, and offers Retry if that reload fails", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsData.current = [
      { credential_id: "cred_acme", name: "Acme login" },
      { credential_id: "cred_other", name: "Other login" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    const box = await screen.findByPlaceholderText("Search credentials...");
    fireEvent.change(box, { target: { value: "acme" } });
    expect(await screen.findByText("Acme login")).toBeTruthy();

    // Erasing the term searches for the empty one. The picker must not vanish mid-edit while that
    // reload is in flight, which would close the dropdown and drop focus.
    credsFail.current = true;
    fireEvent.change(box, { target: { value: "" } });
    expect(screen.getByPlaceholderText("Search credentials...")).toBeTruthy();

    // A failed reset leaves the rows answering "acme" while the box is empty, so it has to say so
    // and stay recoverable — otherwise the picker never returns and creation is the only option.
    expect(
      await screen.findByText(/Couldn't run that search/, {
        selector: ":not(.sr-only)",
      }),
    ).toBeTruthy();
    expect(screen.getByPlaceholderText("Search credentials...")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Acme login" })).toBeNull();

    credsFail.current = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Other login")).toBeTruthy();
    expect(screen.getByText("Acme login")).toBeTruthy();
    expect(screen.queryByText(/Couldn't run that search/)).toBeNull();
    errSpy.mockRestore();
  });

  it("trims the term before searching, so padding neither misses a match nor empties the list", async () => {
    credsData.current = [{ credential_id: "cred_acme", name: "Acme login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Acme login")).toBeTruthy();

    // The route builds its ILIKE pattern from the literal value, so an untrimmed " Acme " would
    // match nothing despite the credential existing.
    const box = screen.getByPlaceholderText("Search credentials...");
    fireEvent.change(box, { target: { value: " Acme " } });
    expect(await screen.findByText("Acme login")).toBeTruthy();
    expect(clientGet).toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: expect.objectContaining({ search: "Acme" }),
      }),
    );

    // An all-space term is no term: it must not be sent as a pattern that matches almost nothing.
    fireEvent.change(box, { target: { value: "   " } });
    expect(await screen.findByText("Acme login")).toBeTruthy();
    expect(clientGet).not.toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: expect.objectContaining({ search: "   " }),
      }),
    );
  });

  it("offers nothing to pick while a typed search is still in flight", async () => {
    const onConnect = vi.fn();
    credsData.current = [{ credential_id: "cred_other", name: "Other login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Other login")).toBeTruthy();

    // Typing leaves the previous rows on hand until the debounce and the request complete. While
    // that is true they answer a different term, and cmdk highlights the first row, so Enter would
    // resume the turn with a login the user never searched for.
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "acme" },
    });
    expect(screen.getByText("Searching…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Other login" })).toBeNull();
    expect(onConnect).not.toHaveBeenCalled();

    credsData.current = [{ credential_id: "cred_acme", name: "Acme login" }];
    fireEvent.click(await screen.findByRole("button", { name: "Acme login" }));
    expect(onConnect).toHaveBeenCalledWith("cred_acme", "Acme login");
    expect(screen.queryByText("Searching…")).toBeNull();
  });

  it("withdraws the previous rows when a search fails, rather than offering them as its result", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsData.current = [{ credential_id: "cred_stale", name: "Earlier row" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="inline-pause"
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(await screen.findByText("Earlier row")).toBeTruthy();
    const liveRegion = screen.getByRole("status");

    credsFail.current = true;
    fireEvent.change(screen.getByPlaceholderText("Search credentials..."), {
      target: { value: "acme" },
    });
    expect(
      await screen.findByText(/Couldn't run that search/, {
        selector: ":not(.sr-only)",
      }),
    ).toBeTruthy();
    // The visible notice renders in the portaled popover, outside any live region, so focus in the
    // search box would otherwise hear nothing. The card's own region, mounted earlier, carries it.
    expect(screen.getByRole("status")).toBe(liveRegion);
    expect(liveRegion.textContent).toBe("Couldn't run that search.");
    // The earlier row answered a different query, so it is no longer offered — nothing selectable
    // can submit a login the user did not search for. The term stays put and retryable.
    expect(screen.queryByText("Earlier row")).toBeNull();
    expect(screen.queryByText("No credentials found.")).toBeNull();
    expect(screen.getByPlaceholderText("Search credentials...")).toBeTruthy();

    credsFail.current = false;
    credsData.current = [{ credential_id: "cred_acme", name: "Acme login" }];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Acme login")).toBeTruthy();
    expect(screen.queryByText(/Couldn't run that search/)).toBeNull();
    errSpy.mockRestore();
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

describe("CredentialCard auto-bound receipt", () => {
  it("names the silently-bound credential in the receipt", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Using credential 'Work login'")).toBeTruthy();
  });

  it("offers a Change picker that routes a pick through onConnect (no third path)", async () => {
    const onConnect = vi.fn();
    credsData.current = [
      { credential_id: "cred_work", name: "Work login" },
      { credential_id: "cred_personal", name: "Personal login" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Personal login" }),
    );
    expect(onConnect).toHaveBeenCalledWith("cred_personal", "Personal login");
  });

  it("stays read-only (no Change) on a scrollback turn where change is not live", async () => {
    credsData.current = [
      { credential_id: "cred_work", name: "Work login" },
      { credential_id: "cred_personal", name: "Personal login" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange={false}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    // The receipt still names the credential, but a scrollback turn offers no Change picker — a pick
    // there would optimistically resolve without an actual continuation behind it.
    expect(screen.getByText("Using credential 'Work login'")).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    // And a read-only receipt does not fetch the org list at all (no per-scrollback-receipt fetch).
    expect(getClientMock).not.toHaveBeenCalled();
  });

  it("hands off to the existing continuing receipt once a change resolves", () => {
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED}
        continued
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(screen.getByText("Continuing with 'HN login'…")).toBeTruthy();
    expect(screen.queryByText(/Using credential/)).toBeNull();
  });

  it("offers an add-credential Change when the bound one is the org's only credential", async () => {
    const onConnect = vi.fn();
    // No OTHER credential to pick, but a correction must stay reachable — Change falls back to adding a
    // new credential (onConnect(undefined) opens the add modal), not a redundant re-pick of the bound one.
    credsData.current = [{ credential_id: "cred_work", name: "Work login" }];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() => expect(getClientMock).toHaveBeenCalled());
    expect(screen.getByText("Using credential 'Work login'")).toBeTruthy();
    // A fallback button (not the searchable combobox), routing to the add modal.
    expect(screen.queryByRole("combobox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(onConnect).toHaveBeenCalledWith(undefined);
  });

  it("keeps Change reachable when the credential list fails to load", async () => {
    const onConnect = vi.fn();
    credsFail.current = true;
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    await waitFor(() => expect(getClientMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    expect(onConnect).toHaveBeenCalledWith(undefined);
  });

  it("says a failed load once when the receipt sits inside the chat's live region", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsFail.current = true;
    // The chat renders this receipt inside each message's role="status" wrapper, which already
    // announces the visible sentence; a hidden copy would make it heard twice.
    render(
      <div role="status" aria-live="polite">
        <CredentialCard
          frame={buildCredentialRequiredFrame()}
          mode="auto-bound"
          autoBound={{ credentialId: "cred_work", name: "Work login" }}
          canChange
          onConnect={vi.fn()}
          onSkip={vi.fn()}
        />
      </div>,
    );
    await screen.findByRole("button", { name: "Retry" });
    expect(
      screen.getAllByText("Couldn't load your other saved logins."),
    ).toHaveLength(1);
    expect(screen.getAllByRole("status")).toHaveLength(1);
    errSpy.mockRestore();
  });

  it("recovers the Change picker from a failed list fetch via Retry", async () => {
    const onConnect = vi.fn();
    credsFail.current = true;
    credsData.current = [
      { credential_id: "cred_other", name: "Other login" },
      { credential_id: "cred_work", name: "Work login" },
    ];
    render(
      <CredentialCard
        frame={buildCredentialRequiredFrame()}
        mode="auto-bound"
        autoBound={{ credentialId: "cred_work", name: "Work login" }}
        canChange
        onConnect={onConnect}
        onSkip={vi.fn()}
      />,
    );
    // Captured before the fetch settles, so the announcement must change this element's text rather
    // than arrive in a freshly mounted one that screen readers would not announce.
    const liveRegion = screen.getByRole("status");
    expect(liveRegion.textContent).toBe("");
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(
      screen.getByText("Couldn't load your other saved logins.", {
        selector: ":not(.sr-only)",
      }),
    ).toBeTruthy();
    expect(screen.getByRole("status")).toBe(liveRegion);
    expect(liveRegion.textContent).toBe(
      "Couldn't load your other saved logins.",
    );
    credsFail.current = false;
    fireEvent.click(retry);
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Other login" }));
    expect(onConnect).toHaveBeenCalledWith("cred_other", "Other login");
  });
});

describe("CredentialCard missing-authenticator ask", () => {
  const frame = CREDENTIAL_REQUIRED_FRAME_BY_REASON.credential_missing_totp;

  function renderUpdateAsk(onUpdateCredential = vi.fn(), onSkip = vi.fn()) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <CredentialCard
          frame={frame}
          mode="inline-pause"
          onConnect={vi.fn()}
          onSkip={onSkip}
          onUpdateCredential={onUpdateCredential}
        />
      </QueryClientProvider>,
    );
  }

  it("names the credential and hands its saved record to the editor, with no picker or input", async () => {
    const onUpdateCredential = vi.fn();
    const onSkip = vi.fn();
    const record = {
      credential_id: "cred_hn",
      name: "HN login",
      credential_type: "password",
      credential: { username: "hn-user" },
    };
    credsData.current = [record];
    const { container } = renderUpdateAsk(onUpdateCredential, onSkip);

    const update = screen.getByRole("button", { name: "Add 2FA method" });
    expect((update as HTMLButtonElement).disabled).toBe(true);
    expect(
      await screen.findByText(
        "Add 2FA to 'HN login' to sign in to https://news.ycombinator.com",
      ),
    ).toBeTruthy();
    fireEvent.click(update);
    expect(onUpdateCredential).toHaveBeenCalledWith(record);
    expect(container.querySelector("input, textarea")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(onSkip).toHaveBeenCalled();
  });

  it("does not claim 2FA was added when the editor saves", () => {
    render(
      <CredentialCard
        frame={frame}
        mode="inline-pause"
        resolvedOutcome={RESOLVED_OUTCOME_CONNECTED}
        onConnect={vi.fn()}
        onSkip={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Saved 'HN login', retrying the verification step"),
    ).toBeTruthy();
    expect(screen.queryByText(/added|updated/)).toBeNull();
  });

  it("offers Retry and Skip, never the generic picker, when the saved record cannot load", async () => {
    credsFail.current = true;
    credsData.current = [{ credential_id: "cred_hn", name: "HN login" }];
    renderUpdateAsk();

    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(screen.getByRole("status").textContent).toBe(
      "Couldn't load this saved login.",
    );
    expect(screen.getByRole("button", { name: "Skip for now" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();

    credsFail.current = false;
    fireEvent.click(retry);
    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: "Add 2FA method",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
  });
});
