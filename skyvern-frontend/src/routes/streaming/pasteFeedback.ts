import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/use-toast";

const PASTE_NOTICE_MS = 2500;

// The bar's container queries resolve against the nearest element carrying these.
export const STREAM_CONTAINER_CLASS =
  "[container-name:stream] [container-type:inline-size]";

export function toastNothingToPaste() {
  toast({
    title: "Nothing to paste",
    description: "Your clipboard has no text to paste.",
  });
}

export function toastClipboardReadFailed() {
  toast({
    variant: "destructive",
    title: "Paste failed",
    description:
      "Skyvern couldn't read your clipboard. Allow clipboard access for this site and try again.",
  });
}

export function usePastedNotice() {
  const [pastedCharacters, setPastedCharacters] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const showPasted = useCallback((text: string) => {
    setPastedCharacters(Array.from(text).length);
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setPastedCharacters(null);
    }, PASTE_NOTICE_MS);
  }, []);

  return [pastedCharacters, showPasted] as const;
}
