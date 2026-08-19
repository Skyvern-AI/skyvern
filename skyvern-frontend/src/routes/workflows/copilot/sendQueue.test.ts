import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { resolveCopilotLiveBrowserReady } from "./browserReadiness";
import { resolveDrainAction, resolveSendAction } from "./sendQueue";

const sendInput = (
  overrides: Partial<Parameters<typeof resolveSendAction>[0]> = {},
): Parameters<typeof resolveSendAction>[0] => ({
  inFlight: false,
  hasQueuedPrompt: false,
  requiresLiveBrowser: false,
  isLiveBrowserReady: false,
  candidate: "hello",
  isDrain: false,
  skipQueue: false,
  ...overrides,
});

describe("resolveSendAction", () => {
  it("returns noop for an empty candidate", () => {
    expect(resolveSendAction(sendInput({ candidate: "   " }))).toBe("noop");
  });

  // A parked turn (awaiting the user) never ends, so a swallowed second send
  // left the composer permanently dead. Rewriting the parked prompt keeps the
  // one-queued-prompt invariant without the dead end.
  it("rewrites the parked prompt when a second send arrives", () => {
    expect(
      resolveSendAction(sendInput({ hasQueuedPrompt: true, isDrain: false })),
    ).toBe("replace_queued");
  });

  it("still returns noop for an empty candidate while a prompt is queued", () => {
    expect(
      resolveSendAction(
        sendInput({ hasQueuedPrompt: true, isDrain: false, candidate: "  " }),
      ),
    ).toBe("noop");
  });

  it("queues for the next turn while a turn is in flight", () => {
    expect(resolveSendAction(sendInput({ inFlight: true }))).toBe(
      "queue_working",
    );
  });

  it("prioritizes the in-flight queue over the live-browser queue", () => {
    expect(
      resolveSendAction(
        sendInput({
          inFlight: true,
          requiresLiveBrowser: true,
          isLiveBrowserReady: false,
        }),
      ),
    ).toBe("queue_working");
  });

  it("queues for the live browser when one is required but not ready", () => {
    expect(
      resolveSendAction(
        sendInput({ requiresLiveBrowser: true, isLiveBrowserReady: false }),
      ),
    ).toBe("queue_live_browser");
  });

  it("sends when the required live browser is ready", () => {
    expect(
      resolveSendAction(
        sendInput({ requiresLiveBrowser: true, isLiveBrowserReady: true }),
      ),
    ).toBe("send");
  });

  it("sends when readiness is resolved from a headless backend session with no display paint", () => {
    const isLiveBrowserReady = resolveCopilotLiveBrowserReady({
      displayReady: false,
      hasBackendSession: true,
      headlessTurnDrainEnabled: true,
    });
    expect(
      resolveSendAction(
        sendInput({ requiresLiveBrowser: true, isLiveBrowserReady }),
      ),
    ).toBe("send");
  });

  it("sends when no live browser is required", () => {
    expect(resolveSendAction(sendInput())).toBe("send");
  });

  it("bypasses the single-queue guard while draining", () => {
    expect(
      resolveSendAction(sendInput({ hasQueuedPrompt: true, isDrain: true })),
    ).toBe("send");
  });

  it("skipQueue forces a send past the live-browser predicate", () => {
    expect(
      resolveSendAction(
        sendInput({
          isDrain: true,
          skipQueue: true,
          requiresLiveBrowser: true,
          isLiveBrowserReady: false,
        }),
      ),
    ).toBe("send");
  });
});

const drainInput = (
  overrides: Partial<Parameters<typeof resolveDrainAction>[0]> = {},
): Parameters<typeof resolveDrainAction>[0] => ({
  queuedReason: null,
  inFlight: false,
  hasLiveBrowserSession: false,
  hasWorkflowPermanentId: true,
  queuedContent: null,
  turnOpeningContent: null,
  turnCompletedNormally: false,
  turnWorkflowMatches: false,
  turnRequestMatches: false,
  ...overrides,
});

describe("resolveDrainAction", () => {
  it("waits when nothing is queued", () => {
    expect(resolveDrainAction(drainInput({ queuedReason: null }))).toBe("wait");
  });

  it("waits while a turn is in flight", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", inFlight: true }),
      ),
    ).toBe("wait");
  });

  it("waits without a workflow permanent id", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasWorkflowPermanentId: false }),
      ),
    ).toBe("wait");
  });

  it("re-queues a working prompt regardless of browser readiness", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasLiveBrowserSession: false }),
      ),
    ).toBe("drain_requeue");
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasLiveBrowserSession: true }),
      ),
    ).toBe("drain_requeue");
  });

  it("skip-queue sends a live_browser prompt only once the session exists", () => {
    expect(
      resolveDrainAction(
        drainInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: true,
        }),
      ),
    ).toBe("drain_skip_queue");
  });

  it("drains a parked live_browser prompt once a backend session is present", () => {
    expect(
      resolveDrainAction(
        drainInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: true,
          inFlight: false,
          hasWorkflowPermanentId: true,
        }),
      ),
    ).toBe("drain_skip_queue");
  });

  const duplicateInput = (
    overrides: Partial<Parameters<typeof resolveDrainAction>[0]> = {},
  ) =>
    drainInput({
      queuedReason: "working",
      queuedContent: "  build me a workflow  ",
      turnOpeningContent: "build me a workflow",
      turnCompletedNormally: true,
      turnWorkflowMatches: true,
      turnRequestMatches: true,
      ...overrides,
    });

  it("drops a queued prompt identical to the message that opened the finished turn", () => {
    expect(resolveDrainAction(duplicateInput())).toBe("drop_duplicate");
  });

  it("drains an identical queued prompt when the turn did not complete normally", () => {
    expect(
      resolveDrainAction(duplicateInput({ turnCompletedNormally: false })),
    ).toBe("drain_requeue");
  });

  it("drains a queued prompt whose text differs from the turn-opening message", () => {
    expect(
      resolveDrainAction(duplicateInput({ queuedContent: "something else" })),
    ).toBe("drain_requeue");
  });

  it("drains a queued prompt that only nearly repeats the turn-opening message", () => {
    expect(
      resolveDrainAction(
        duplicateInput({ queuedContent: "build me a workflow." }),
      ),
    ).toBe("drain_requeue");
    expect(
      resolveDrainAction(
        duplicateInput({ queuedContent: "Build me a workflow" }),
      ),
    ).toBe("drain_requeue");
    expect(
      resolveDrainAction(
        duplicateInput({ queuedContent: "build me  a workflow" }),
      ),
    ).toBe("drain_requeue");
  });

  it("drains an identical queued prompt when the queued send would not be the same request", () => {
    expect(
      resolveDrainAction(duplicateInput({ turnRequestMatches: false })),
    ).toBe("drain_requeue");
  });

  it("drains an identical queued prompt when the turn opened on another workflow", () => {
    expect(
      resolveDrainAction(duplicateInput({ turnWorkflowMatches: false })),
    ).toBe("drain_requeue");
  });

  it("leaves a live_browser prompt on the skip-queue path even when it is identical", () => {
    expect(
      resolveDrainAction(
        duplicateInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: true,
        }),
      ),
    ).toBe("drain_skip_queue");
  });

  it("waits for the session before draining a live_browser prompt", () => {
    expect(
      resolveDrainAction(
        drainInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: false,
        }),
      ),
    ).toBe("wait");
  });
});

// Offline replay of the recorded duplicate-send incident (SKY-12192). Runs
// only when DUP_SEND_DUMP points at the captured evidence root.
const dumpRoot = process.env.DUP_SEND_DUMP;

type ChatPostEvent = { t: string; kind: string; message: string };

type ChatHistoryEntry = {
  sender: "user" | "ai";
  content: string;
  audio_artifact_id: string | null;
  created_at: string;
  narrative_payload: { cancelled?: boolean; terminal?: string } | null;
};

function readJson<T>(root: string, name: string): T {
  return JSON.parse(readFileSync(join(root, name), "utf8")) as T;
}

// Rebuilds the drain inputs from the captured duplicate-send run: the two
// chat_post payloads, and the narrative the first turn ended on.
function drainInputFromDump(root: string) {
  const events = readJson<ChatPostEvent[]>(root, "network_events.json");
  const posts = events.filter((event) => event.kind === "chat_post");
  expect(posts).toHaveLength(2);

  const history = readJson<{ chat_history: ChatHistoryEntry[] }>(
    root,
    "chat_history.json",
  ).chat_history;
  const userEntries = history.filter((entry) => entry.sender === "user");
  expect(userEntries.map((entry) => entry.content)).toEqual(
    posts.map((post) => post.message),
  );
  const noAudio = userEntries.every(
    (entry) => entry.audio_artifact_id === null,
  );

  const narrative = history.find(
    (entry) => entry.sender === "ai",
  )?.narrative_payload;
  if (!narrative) {
    throw new Error("captured run has no ai narrative payload");
  }

  // The stored rows are the independent record that the reply landed before the
  // duplicate post; the narrative is what the browser itself saw.
  const dbRows = readFileSync(join(root, "db_rows.txt"), "utf8");
  const messageRows = dbRows
    .split("\n")
    .filter((row) => /^ wccm_/.test(row))
    .map((row) => row.split("|").map((cell) => cell.trim()));
  expect(messageRows).toHaveLength(4);
  const chatIds = new Set(messageRows.map((cells) => cells[1]));
  expect(chatIds.size).toBe(1);

  const secondPostAt = Date.parse(posts[1]!.t);
  const aiRowBeforeSecondPost = dbRows
    .split("\n")
    .filter((row) => / \| ai +\| /.test(row))
    .some((row) => {
      const stamp = row.split("|")[3]?.trim();
      return (
        stamp !== undefined &&
        Date.parse(`${stamp.replace(" ", "T")}Z`) <= secondPostAt
      );
    });
  expect(aiRowBeforeSecondPost).toBe(true);

  return {
    queuedReason: "working" as const,
    inFlight: false,
    hasLiveBrowserSession: false,
    hasWorkflowPermanentId: true,
    queuedContent: posts[1]!.message,
    turnOpeningContent: posts[0]!.message,
    turnCompletedNormally:
      narrative.cancelled === false && narrative.terminal !== "error",
    // Both posts landed on the one chat id asserted above, which the frontend
    // only opens per workflow.
    turnWorkflowMatches: chatIds.size === 1,
    // Only the audio leg is recoverable here; the packet records no mode, fix
    // origin, block target or code_block, so the component tests cover those.
    turnRequestMatches: noAudio,
  };
}

describe.skipIf(!dumpRoot)(
  "resolveDrainAction — captured duplicate send",
  () => {
    it("records the duplicate the captured run fired", () => {
      const events = readJson<ChatPostEvent[]>(
        dumpRoot!,
        "network_events.json",
      );
      const posts = events.filter((event) => event.kind === "chat_post");
      expect(posts.map((post) => post.message)).toEqual([
        posts[0]!.message,
        posts[0]!.message,
      ]);
    });

    it("drops the second chat_post the captured run fired", () => {
      const input = drainInputFromDump(dumpRoot!);
      expect(input.turnCompletedNormally).toBe(true);
      expect(input.queuedContent).toBe(input.turnOpeningContent);
      expect(resolveDrainAction(input)).toBe("drop_duplicate");
    });
  },
);
