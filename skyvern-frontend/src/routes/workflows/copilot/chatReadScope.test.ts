import { describe, expect, it } from "vitest";

import { captureChatScope } from "./chatReadScope";

const scopeRefs = (chatId: string | null) => ({
  chatId: { current: chatId },
  navEpoch: { current: 0 },
  sendEpoch: { current: 0 },
});

describe("captureChatScope", () => {
  it("keeps a result when nothing moved", () => {
    const refs = scopeRefs("chat-1");
    const scope = captureChatScope(refs);
    expect(scope.chatId).toBe("chat-1");
    expect(scope.navigated()).toBe(false);
    expect(scope.outlived()).toBe(false);
  });

  // The chat id ref is synced by a passive effect, so right after a navigation it still names the
  // old chat. Only the epoch says the ground moved.
  it("discards a result once navigation starts, before the chat id ref catches up", () => {
    const refs = scopeRefs("chat-1");
    const scope = captureChatScope(refs);
    refs.navEpoch.current += 1;
    expect(scope.navigated()).toBe(true);
    expect(scope.outlived()).toBe(true);
  });

  it("discards a result when the chat id names another chat", () => {
    const refs = scopeRefs("chat-1");
    const scope = captureChatScope(refs);
    refs.chatId.current = "chat-2";
    expect(scope.navigated()).toBe(true);
  });

  it("treats a chat resolving its first id as the same chat", () => {
    const refs = scopeRefs(null);
    const scope = captureChatScope(refs);
    refs.chatId.current = "chat-1";
    expect(scope.navigated()).toBe(false);
  });

  it("outlives a send without navigating", () => {
    const refs = scopeRefs("chat-1");
    const scope = captureChatScope(refs);
    refs.sendEpoch.current += 1;
    expect(scope.navigated()).toBe(false);
    expect(scope.outlived()).toBe(true);
  });
});
