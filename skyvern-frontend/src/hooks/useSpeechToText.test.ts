import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isSpeechRecognitionSupported,
  useSpeechToText,
} from "./useSpeechToText";

type ResultHandler = (event: {
  resultIndex: number;
  results: Array<{ isFinal: boolean; transcript: string }>;
}) => void;

type ErrorHandler = (event: { error: string; message?: string }) => void;

class MockSpeechRecognition {
  continuous = false;
  interimResults = false;
  lang = "";
  onresult: ResultHandler | null = null;
  onerror: ErrorHandler | null = null;
  onend: (() => void) | null = null;

  private resultList: Array<{ isFinal: boolean; transcript: string }> = [];

  start = vi.fn(() => {
    MockSpeechRecognition.lastInstance = this;
    this.resultList = [];
  });

  stop = vi.fn(() => {
    this.onend?.();
  });

  abort = vi.fn(() => {
    this.onend?.();
  });

  static lastInstance: MockSpeechRecognition | null = null;

  emitResult(transcript: string, isFinal = true, resultIndex?: number) {
    const index = resultIndex ?? this.resultList.length;
    this.resultList[index] = { isFinal, transcript };

    const results = {
      length: this.resultList.length,
      ...Object.fromEntries(
        this.resultList.map((entry, i) => [
          i,
          {
            length: 1,
            isFinal: entry.isFinal,
            0: { transcript: entry.transcript },
          },
        ]),
      ),
    };

    this.onresult?.({
      resultIndex: index,
      results: results as never,
    });
  }

  emitError(error: string) {
    this.onerror?.({ error });
  }

  emitEnd() {
    this.onend?.();
  }
}

describe("isSpeechRecognitionSupported", () => {
  afterEach(() => {
    delete (window as { SpeechRecognition?: unknown }).SpeechRecognition;
    delete (window as { webkitSpeechRecognition?: unknown })
      .webkitSpeechRecognition;
  });

  it("returns true when SpeechRecognition exists", () => {
    (
      window as { SpeechRecognition?: typeof MockSpeechRecognition }
    ).SpeechRecognition = MockSpeechRecognition;
    expect(isSpeechRecognitionSupported()).toBe(true);
  });

  it("returns false when SpeechRecognition is missing", () => {
    expect(isSpeechRecognitionSupported()).toBe(false);
  });
});

describe("useSpeechToText", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockSpeechRecognition.lastInstance = null;
    (
      window as { SpeechRecognition?: typeof MockSpeechRecognition }
    ).SpeechRecognition = MockSpeechRecognition;
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (window as { SpeechRecognition?: unknown }).SpeechRecognition;
    delete (window as { webkitSpeechRecognition?: unknown })
      .webkitSpeechRecognition;
  });

  it("reports support from the browser API", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );
    expect(result.current.isSupported).toBe(true);
  });

  it("starts and stops recognition", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.start();
    });

    expect(result.current.isListening).toBe(true);
    expect(MockSpeechRecognition.lastInstance?.start).toHaveBeenCalled();
    expect(MockSpeechRecognition.lastInstance?.continuous).toBe(true);
    expect(MockSpeechRecognition.lastInstance?.interimResults).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(result.current.isListening).toBe(false);
    expect(MockSpeechRecognition.lastInstance?.stop).toHaveBeenCalled();
  });

  it("toggles listening on and off", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.toggle();
    });
    expect(result.current.isListening).toBe(true);

    act(() => {
      result.current.toggle();
    });
    expect(result.current.isListening).toBe(false);
  });

  it("appends final transcript chunks with spacing", () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "existing prompt",
        onTranscript,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hello world");
    });
    expect(onTranscript).toHaveBeenLastCalledWith(
      "existing prompt hello world",
    );

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("more text", true, 1);
    });
    expect(onTranscript).toHaveBeenLastCalledWith(
      "existing prompt hello world more text",
    );
  });

  it("starts from an empty base text", () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "",
        onTranscript,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("first phrase");
    });
    expect(onTranscript).toHaveBeenLastCalledWith("first phrase");
  });

  it("surfaces permission errors", () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitError("not-allowed");
    });

    expect(onError).toHaveBeenCalledWith(
      "Microphone access was denied. Allow microphone permission and try again.",
    );
    expect(result.current.isListening).toBe(false);
  });

  it("stops when enabled becomes false", () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useSpeechToText({
          onTranscript: vi.fn(),
          enabled,
        }),
      { initialProps: { enabled: true } },
    );

    act(() => {
      result.current.start();
    });
    expect(result.current.isListening).toBe(true);

    rerender({ enabled: false });

    expect(result.current.isListening).toBe(false);
  });

  it("cleans up active recognition on unmount", () => {
    const { result, unmount } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.start();
    });

    const instance = MockSpeechRecognition.lastInstance;
    unmount();

    expect(instance?.abort).toHaveBeenCalled();
  });

  it("updates transcript with interim results", () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "base",
        onTranscript,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(onTranscript).toHaveBeenLastCalledWith("base hel");
  });

  it("pulses isHearingSpeech while interim speech is detected", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(result.current.isHearingSpeech).toBe(true);

    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(result.current.isHearingSpeech).toBe(false);
  });

  it("clears isHearingSpeech when dictation stops", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(result.current.isHearingSpeech).toBe(true);

    act(() => {
      result.current.stop();
    });
    expect(result.current.isHearingSpeech).toBe(false);
  });

  it("restarts recognition when the browser ends the session while listening", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    act(() => {
      result.current.start();
    });

    const instance = MockSpeechRecognition.lastInstance;
    expect(instance?.start).toHaveBeenCalledTimes(1);

    act(() => {
      instance?.emitEnd();
    });

    expect(instance?.start).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(300);
    });

    expect(instance?.start).toHaveBeenCalledTimes(2);
    expect(result.current.isListening).toBe(true);
  });

  it("stops listening after too many rapid auto-restarts", () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    act(() => {
      result.current.start();
    });

    const instance = MockSpeechRecognition.lastInstance;
    expect(instance).not.toBeNull();

    for (let i = 0; i < 9; i += 1) {
      act(() => {
        instance?.emitEnd();
        vi.advanceTimersByTime(300);
      });
    }

    expect(result.current.isListening).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      "Voice input stopped after repeated failures. Try again.",
    );
  });

  it("ignores no-speech errors while listening", () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitError("no-speech");
    });

    expect(onError).not.toHaveBeenCalled();
    expect(result.current.isListening).toBe(true);
  });

  it("does not start when enabled is false", () => {
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        enabled: false,
      }),
    );

    act(() => {
      result.current.start();
    });

    expect(result.current.isListening).toBe(false);
    expect(MockSpeechRecognition.lastInstance).toBeNull();
  });

  it("uses cached finalized chunks for results before resultIndex", () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "",
        onTranscript,
      }),
    );

    act(() => {
      result.current.start();
    });

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hello world");
    });
    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("more text", true, 1);
    });

    expect(onTranscript).toHaveBeenLastCalledWith("hello world more text");
  });
});
