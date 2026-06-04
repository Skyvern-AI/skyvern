import {
  RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

const DEFAULT_THRESHOLD_PX = 48;

interface UseStickToBottomOptions {
  enabled?: boolean;
  thresholdPx?: number;
}

interface UseStickToBottomResult<T extends HTMLElement> {
  scrollRef: RefObject<T>;
  isPinned: boolean;
  jumpToLatest: () => void;
  repin: () => void;
}

// Follow-the-frontier scroll for a streaming chat pane: re-scrolls to the
// bottom whenever `signature` changes while pinned, disengaging when the user
// scrolls up past `thresholdPx` and re-engaging at the bottom / on jumpToLatest.
export function useStickToBottom<T extends HTMLElement>(
  signature: number | string,
  options: UseStickToBottomOptions = {},
): UseStickToBottomResult<T> {
  const { enabled = true, thresholdPx = DEFAULT_THRESHOLD_PX } = options;
  const scrollRef = useRef<T>(null);
  const pinnedRef = useRef(true);
  const [isPinned, setIsPinned] = useState(true);

  const setPinned = useCallback((next: boolean) => {
    pinnedRef.current = next;
    setIsPinned((prev) => (prev === next ? prev : next));
  }, []);

  // Instant, not smooth: a smooth animation emits intermediate scroll events
  // that the listener below would misread as a user scroll-up (self-unpinning).
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight });
  }, []);

  const repin = useCallback(() => {
    setPinned(true);
  }, [setPinned]);

  const jumpToLatest = useCallback(() => {
    setPinned(true);
    scrollToBottom();
  }, [scrollToBottom, setPinned]);

  // `enabled` is in the deps so reopen rebinds the listener to the fresh node —
  // the consumer unmounts the scroll container while the panel is closed.
  useEffect(() => {
    if (!enabled) return;
    const el = scrollRef.current;
    if (!el) return;
    const handleScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      setPinned(distance <= thresholdPx);
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [enabled, setPinned, thresholdPx]);

  // Reopen must re-pin AND scroll: the fresh node starts at the top, and a user
  // who scrolled up before closing left pinnedRef false, so the follow effect
  // below would otherwise skip and reopen stuck at the top.
  useLayoutEffect(() => {
    if (!enabled) return;
    setPinned(true);
    scrollToBottom();
  }, [enabled, scrollToBottom, setPinned]);

  useLayoutEffect(() => {
    if (!enabled || !pinnedRef.current) return;
    scrollToBottom();
  }, [signature, enabled, scrollToBottom]);

  return { scrollRef, isPinned, jumpToLatest, repin };
}

// Visible-content fingerprint that changes on any rendered growth/move. The
// narrative is serialized rather than counted because `appendCapped` rotates
// tail content at the cap, so its length stops changing mid-run.
export function computeFollowSignature(
  messages: ReadonlyArray<{ id: string; content: string }>,
  narrative: unknown,
  isLoading: boolean,
  isLoadingHistory: boolean,
  queuedPrompt: { id: string; reason: string } | null,
  hasProposal: boolean,
): string {
  const last = messages[messages.length - 1];
  const messagePart = `${messages.length}:${last?.id ?? ""}:${last?.content.length ?? 0}`;
  const footerPart = `${isLoading ? 1 : 0}:${isLoadingHistory ? 1 : 0}:${queuedPrompt?.id ?? ""}:${queuedPrompt?.reason ?? ""}:${hasProposal ? 1 : 0}`;
  return `${messagePart}|${footerPart}|${JSON.stringify(narrative)}`;
}
