import { useCallback, useEffect, useRef, useState } from "react";

interface SpeechRecognitionResultItem {
  transcript: string;
}

interface SpeechRecognitionResultList {
  readonly length: number;
  [index: number]: {
    readonly length: number;
    [index: number]: SpeechRecognitionResultItem;
    isFinal: boolean;
  };
}

interface SpeechRecognitionEventLike extends Event {
  resultIndex: number;
  results: SpeechRecognitionResultList;
}

interface SpeechRecognitionErrorEventLike extends Event {
  error: string;
  message?: string;
}

interface SpeechRecognitionLike extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

declare global {
  interface Window {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  }
}

const HEARING_PULSE_MS = 400;
const MIN_RESTART_INTERVAL_MS = 300;
const MAX_RAPID_RESTARTS = 8;

function getSpeechRecognitionConstructor():
  | SpeechRecognitionConstructor
  | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  return window.SpeechRecognition ?? window.webkitSpeechRecognition;
}

export function isSpeechRecognitionSupported(): boolean {
  return getSpeechRecognitionConstructor() !== undefined;
}

function mergeTranscript(baseText: string, dictatedText: string): string {
  const base = baseText.trimEnd();
  const dictated = dictatedText.trim();
  if (!dictated) {
    return baseText;
  }
  if (!base) {
    return dictated;
  }
  return `${base} ${dictated}`;
}

function buildSessionTranscript(
  results: SpeechRecognitionResultList,
  resultIndex: number,
  finalizedChunks: Map<number, string>,
): {
  finalText: string;
  interimText: string;
} {
  const finalParts: string[] = [];

  for (let i = 0; i < resultIndex; i += 1) {
    const cached = finalizedChunks.get(i);
    if (cached) {
      finalParts.push(cached);
    }
  }

  const interimParts: string[] = [];
  for (let i = resultIndex; i < results.length; i += 1) {
    const result = results[i];
    if (!result) {
      continue;
    }
    const chunk = (result[0]?.transcript ?? "").trim();
    if (!chunk) {
      continue;
    }
    if (result.isFinal) {
      finalParts.push(chunk);
      finalizedChunks.set(i, chunk);
    } else {
      interimParts.push(chunk);
    }
  }
  return {
    finalText: finalParts.join(" "),
    interimText: interimParts.join(" "),
  };
}

function combineDictated(finalText: string, interimText: string): string {
  if (finalText && interimText) {
    return `${finalText} ${interimText}`;
  }
  return finalText || interimText;
}

export interface UseSpeechToTextOptions {
  /** Snapshot of textarea content when dictation starts. */
  getBaseText?: () => string;
  onTranscript: (text: string) => void;
  onError?: (message: string) => void;
  lang?: string;
  /** When false, active recognition is stopped (e.g. panel closed). */
  enabled?: boolean;
}

export interface UseSpeechToTextResult {
  isSupported: boolean;
  isListening: boolean;
  /** Briefly true while interim speech is being recognized. */
  isHearingSpeech: boolean;
  start: () => void;
  stop: () => void;
  toggle: () => void;
}

const ERROR_MESSAGES: Record<string, string> = {
  "not-allowed":
    "Microphone access was denied. Allow microphone permission and try again.",
  "service-not-allowed":
    "Microphone access was denied. Allow microphone permission and try again.",
  network: "Voice input failed due to a network error. Try again.",
  "audio-capture": "No microphone was found. Check your device and try again.",
};

function errorMessageForCode(error: string): string {
  return ERROR_MESSAGES[error] ?? "Voice input failed. Try again.";
}

export function useSpeechToText(
  options: UseSpeechToTextOptions,
): UseSpeechToTextResult {
  const { getBaseText, onTranscript, onError, lang, enabled = true } = options;

  const isSupported = isSpeechRecognitionSupported();
  const [isListening, setIsListening] = useState(false);
  const [isHearingSpeech, setIsHearingSpeech] = useState(false);

  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const baseTextRef = useRef("");
  const isListeningRef = useRef(false);
  const shouldKeepListeningRef = useRef(false);
  const enabledRef = useRef(enabled);
  const finalizedChunksRef = useRef<Map<number, string>>(new Map());
  const hearingTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restartTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastStartAttemptAtRef = useRef(0);
  const rapidRestartCountRef = useRef(0);

  const onTranscriptRef = useRef(onTranscript);
  const onErrorRef = useRef(onError);
  const getBaseTextRef = useRef(getBaseText);

  useEffect(() => {
    onTranscriptRef.current = onTranscript;
    onErrorRef.current = onError;
    getBaseTextRef.current = getBaseText;
  }, [onTranscript, onError, getBaseText]);

  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  const clearHearingSpeech = useCallback(() => {
    if (hearingTimeoutRef.current !== null) {
      clearTimeout(hearingTimeoutRef.current);
      hearingTimeoutRef.current = null;
    }
    setIsHearingSpeech(false);
  }, []);

  const markHearingSpeech = useCallback(() => {
    setIsHearingSpeech(true);
    if (hearingTimeoutRef.current !== null) {
      clearTimeout(hearingTimeoutRef.current);
    }
    hearingTimeoutRef.current = setTimeout(() => {
      setIsHearingSpeech(false);
      hearingTimeoutRef.current = null;
    }, HEARING_PULSE_MS);
  }, []);

  const clearRestartTimeout = useCallback(() => {
    if (restartTimeoutRef.current !== null) {
      clearTimeout(restartTimeoutRef.current);
      restartTimeoutRef.current = null;
    }
  }, []);

  const teardownRecognition = useCallback(() => {
    clearRestartTimeout();
    const recognition = recognitionRef.current;
    if (!recognition) {
      return;
    }
    recognition.onresult = null;
    recognition.onerror = null;
    recognition.onend = null;
    try {
      recognition.abort();
    } catch {
      // ignore abort errors during cleanup
    }
    recognitionRef.current = null;
  }, [clearRestartTimeout]);

  const stop = useCallback(() => {
    shouldKeepListeningRef.current = false;
    isListeningRef.current = false;
    setIsListening(false);
    clearHearingSpeech();
    clearRestartTimeout();
    const recognition = recognitionRef.current;
    if (recognition) {
      try {
        recognition.stop();
      } catch {
        teardownRecognition();
      }
    }
  }, [clearHearingSpeech, clearRestartTimeout, teardownRecognition]);

  const start = useCallback(() => {
    if (!isSupported || !enabledRef.current || isListeningRef.current) {
      return;
    }

    const SpeechRecognitionCtor = getSpeechRecognitionConstructor();
    if (!SpeechRecognitionCtor) {
      return;
    }

    teardownRecognition();
    clearHearingSpeech();
    finalizedChunksRef.current.clear();
    rapidRestartCountRef.current = 0;

    baseTextRef.current = getBaseTextRef.current?.() ?? "";

    const recognition = new SpeechRecognitionCtor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang =
      lang ?? (typeof navigator !== "undefined" ? navigator.language : "en-US");

    recognition.onresult = (event: SpeechRecognitionEventLike) => {
      const { finalText, interimText } = buildSessionTranscript(
        event.results,
        event.resultIndex,
        finalizedChunksRef.current,
      );
      const dictated = combineDictated(finalText, interimText);
      onTranscriptRef.current(mergeTranscript(baseTextRef.current, dictated));
      rapidRestartCountRef.current = 0;
      if (interimText) {
        markHearingSpeech();
      }
    };

    recognition.onerror = (event: SpeechRecognitionErrorEventLike) => {
      if (event.error === "aborted") {
        return;
      }
      if (event.error === "no-speech" && shouldKeepListeningRef.current) {
        return;
      }
      shouldKeepListeningRef.current = false;
      isListeningRef.current = false;
      setIsListening(false);
      clearHearingSpeech();
      onErrorRef.current?.(errorMessageForCode(event.error));
      teardownRecognition();
    };

    const attemptAutoRestart = (): boolean => {
      if (
        !shouldKeepListeningRef.current ||
        recognitionRef.current !== recognition
      ) {
        return false;
      }

      rapidRestartCountRef.current += 1;
      if (rapidRestartCountRef.current > MAX_RAPID_RESTARTS) {
        shouldKeepListeningRef.current = false;
        isListeningRef.current = false;
        setIsListening(false);
        clearHearingSpeech();
        recognitionRef.current = null;
        onErrorRef.current?.(
          "Voice input stopped after repeated failures. Try again.",
        );
        return false;
      }

      try {
        lastStartAttemptAtRef.current = Date.now();
        recognition.start();
        return true;
      } catch {
        shouldKeepListeningRef.current = false;
        return false;
      }
    };

    recognition.onend = () => {
      if (
        shouldKeepListeningRef.current &&
        recognitionRef.current === recognition
      ) {
        const elapsed = Date.now() - lastStartAttemptAtRef.current;
        if (elapsed < MIN_RESTART_INTERVAL_MS) {
          clearRestartTimeout();
          restartTimeoutRef.current = setTimeout(() => {
            restartTimeoutRef.current = null;
            attemptAutoRestart();
          }, MIN_RESTART_INTERVAL_MS - elapsed);
          return;
        }

        if (attemptAutoRestart()) {
          return;
        }
      }

      isListeningRef.current = false;
      setIsListening(false);
      clearHearingSpeech();
      recognitionRef.current = null;
    };

    recognitionRef.current = recognition;

    try {
      lastStartAttemptAtRef.current = Date.now();
      recognition.start();
      shouldKeepListeningRef.current = true;
      isListeningRef.current = true;
      setIsListening(true);
    } catch {
      shouldKeepListeningRef.current = false;
      isListeningRef.current = false;
      setIsListening(false);
      clearHearingSpeech();
      teardownRecognition();
      onErrorRef.current?.("Voice input failed to start. Try again.");
    }
  }, [
    clearHearingSpeech,
    clearRestartTimeout,
    isSupported,
    lang,
    markHearingSpeech,
    teardownRecognition,
  ]);

  const toggle = useCallback(() => {
    if (isListeningRef.current) {
      stop();
    } else {
      start();
    }
  }, [start, stop]);

  useEffect(() => {
    if (!enabled && isListeningRef.current) {
      stop();
    }
  }, [enabled, stop]);

  useEffect(() => {
    return () => {
      shouldKeepListeningRef.current = false;
      isListeningRef.current = false;
      clearHearingSpeech();
      teardownRecognition();
    };
  }, [clearHearingSpeech, teardownRecognition]);

  return {
    isSupported,
    isListening,
    isHearingSpeech,
    start,
    stop,
    toggle,
  };
}
