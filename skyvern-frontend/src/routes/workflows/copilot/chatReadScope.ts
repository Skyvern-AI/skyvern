type Ref<T> = { current: T };

export type ChatScopeRefs = {
  chatId: Ref<string | null>;
  // Bumped synchronously when navigation applies, so it moves a commit before the chat id ref,
  // which a passive effect syncs.
  navEpoch: Ref<number>;
  sendEpoch: Ref<number>;
};

export type ChatReadScope = {
  chatId: string | null;
  navigated: () => boolean;
  outlived: () => boolean;
};

// An async result belongs only to the chat that started it. Capture before the await; after it,
// a result whose scope navigated (or, for `outlived`, also saw a newer send) is discarded.
// A null chat id that later resolves to an id is the same chat, not a switch.
export function captureChatScope(refs: ChatScopeRefs): ChatReadScope {
  const chatId = refs.chatId.current;
  const navEpoch = refs.navEpoch.current;
  const sendEpoch = refs.sendEpoch.current;
  const navigated = () =>
    refs.navEpoch.current !== navEpoch ||
    (chatId !== null && refs.chatId.current !== chatId);
  return {
    chatId,
    navigated,
    outlived: () => navigated() || refs.sendEpoch.current !== sendEpoch,
  };
}
