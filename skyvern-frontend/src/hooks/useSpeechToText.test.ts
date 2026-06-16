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

class MockMediaRecorder {
  static lastInstance: MockMediaRecorder | null = null;

  mimeType = "audio/webm";
  state: RecordingState = "inactive";
  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(public stream: MediaStream) {
    MockMediaRecorder.lastInstance = this;
  }

  start = vi.fn(() => {
    this.state = "recording";
  });

  stop = vi.fn(() => {
    this.state = "inactive";
    this.ondataavailable?.({
      data: new Blob(["audio"], { type: "audio/webm" }),
    } as BlobEvent);
    this.onstop?.();
  });

  emitData(data: Blob) {
    this.ondataavailable?.({ data } as BlobEvent);
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

const mockGetUserMedia = vi.fn().mockResolvedValue({
  getTracks: () => [{ stop: vi.fn() }],
});

async function startSpeech(
  start: () => void,
): Promise<MockSpeechRecognition | null> {
  await act(async () => {
    start();
    await Promise.resolve();
  });
  return MockSpeechRecognition.lastInstance;
}

describe("useSpeechToText", () => {
  const trackStop = vi.fn();

  beforeEach(() => {
    vi.useFakeTimers();
    MockSpeechRecognition.lastInstance = null;
    MockMediaRecorder.lastInstance = null;
    trackStop.mockClear();
    mockGetUserMedia.mockResolvedValue({
      getTracks: () => [{ stop: trackStop }],
    });
    (
      window as { SpeechRecognition?: typeof MockSpeechRecognition }
    ).SpeechRecognition = MockSpeechRecognition;
    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: MockMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: mockGetUserMedia },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (window as { SpeechRecognition?: unknown }).SpeechRecognition;
    delete (window as { webkitSpeechRecognition?: unknown })
      .webkitSpeechRecognition;
    delete (globalThis as { MediaRecorder?: unknown }).MediaRecorder;
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  });

  it("reports support from the browser API", () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );
    expect(result.current.isSupported).toBe(true);
  });

  it("starts and stops recognition", async () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    const instance = await startSpeech(() => result.current.start());

    expect(mockGetUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(result.current.isListening).toBe(true);
    expect(instance?.start).toHaveBeenCalled();
    expect(instance?.continuous).toBe(true);
    expect(instance?.interimResults).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(result.current.isListening).toBe(false);
    expect(instance?.stop).toHaveBeenCalled();
  });

  it("captures an audio blob while dictating", async () => {
    const onAudioCaptured = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onAudioCaptured,
      }),
    );

    act(() => {
      result.current.start();
    });
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      await result.current.stop();
    });

    expect(onAudioCaptured).toHaveBeenCalledTimes(1);
    const audioBlob = onAudioCaptured.mock.calls[0]?.[0] as Blob;
    expect(audioBlob.type).toBe("audio/webm");
    expect(result.current.takeAudioBlob()).toBe(audioBlob);
    expect(trackStop).toHaveBeenCalled();
  });

  it("keeps buffered audio chunks when the recorder is already inactive", async () => {
    const onAudioCaptured = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onAudioCaptured,
      }),
    );

    act(() => {
      result.current.start();
    });
    await act(async () => {
      await Promise.resolve();
    });

    const recorder = MockMediaRecorder.lastInstance;
    const audioChunk = new Blob(["partial-audio"], { type: "audio/webm" });
    act(() => {
      recorder?.emitData(audioChunk);
      if (recorder) {
        recorder.state = "inactive";
      }
    });

    let stoppedBlob: Blob | null = null;
    await act(async () => {
      stoppedBlob = await result.current.stop();
    });

    const capturedBlob = stoppedBlob as unknown as Blob;
    expect(capturedBlob).toBeInstanceOf(Blob);
    expect(capturedBlob.type).toBe("audio/webm");
    expect(await capturedBlob.text()).toBe("partial-audio");
    expect(onAudioCaptured).toHaveBeenCalledWith(capturedBlob);
    expect(recorder?.stop).not.toHaveBeenCalled();
    expect(trackStop).toHaveBeenCalled();
  });

  it("toggles listening on and off", async () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    await startSpeech(() => result.current.toggle());
    expect(result.current.isListening).toBe(true);

    act(() => {
      result.current.toggle();
    });
    expect(result.current.isListening).toBe(false);
  });

  it("appends final transcript chunks with spacing", async () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "existing prompt",
        onTranscript,
      }),
    );

    await startSpeech(() => result.current.start());

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

  it("starts from an empty base text", async () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "",
        onTranscript,
      }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("first phrase");
    });
    expect(onTranscript).toHaveBeenLastCalledWith("first phrase");
  });

  it("surfaces permission errors when microphone access is denied", async () => {
    const onError = vi.fn();
    mockGetUserMedia.mockRejectedValue(
      new DOMException("Permission denied", "NotAllowedError"),
    );
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    await startSpeech(() => result.current.start());

    expect(onError).toHaveBeenCalledWith(
      "Microphone access was denied. Allow microphone permission and try again.",
    );
    expect(result.current.isListening).toBe(false);
    expect(MockSpeechRecognition.lastInstance).toBeNull();
  });

  it("surfaces no-microphone errors from getUserMedia", async () => {
    const onError = vi.fn();
    mockGetUserMedia.mockRejectedValue(
      new DOMException("Requested device not found", "NotFoundError"),
    );
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    await startSpeech(() => result.current.start());

    expect(onError).toHaveBeenCalledWith(
      "No microphone was found. Check your device and try again.",
    );
    expect(result.current.isListening).toBe(false);
    expect(MockSpeechRecognition.lastInstance).toBeNull();
  });

  it("surfaces speech recognition permission errors", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitError("not-allowed");
    });

    expect(onError).toHaveBeenCalledWith(
      "Microphone access was denied. Allow microphone permission and try again.",
    );
    expect(result.current.isListening).toBe(false);
  });

  it("stops when enabled becomes false", async () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useSpeechToText({
          onTranscript: vi.fn(),
          enabled,
        }),
      { initialProps: { enabled: true } },
    );

    await startSpeech(() => result.current.start());
    expect(result.current.isListening).toBe(true);

    rerender({ enabled: false });

    expect(result.current.isListening).toBe(false);
  });

  it("cleans up active recognition on unmount", async () => {
    const { result, unmount } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    await startSpeech(() => result.current.start());

    const instance = MockSpeechRecognition.lastInstance;
    unmount();

    expect(instance?.abort).toHaveBeenCalled();
  });

  it("updates transcript with interim results", async () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "base",
        onTranscript,
      }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(onTranscript).toHaveBeenLastCalledWith("base hel");
  });

  it("pulses isHearingSpeech while interim speech is detected", async () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(result.current.isHearingSpeech).toBe(true);

    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(result.current.isHearingSpeech).toBe(false);
  });

  it("clears isHearingSpeech when dictation stops", async () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hel", false);
    });
    expect(result.current.isHearingSpeech).toBe(true);

    act(() => {
      result.current.stop();
    });
    expect(result.current.isHearingSpeech).toBe(false);
  });

  it("restarts recognition when the browser ends the session while listening", async () => {
    const { result } = renderHook(() =>
      useSpeechToText({ onTranscript: vi.fn() }),
    );

    await startSpeech(() => result.current.start());

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

  it("stops listening after too many rapid auto-restarts", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    await startSpeech(() => result.current.start());

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

  it("ignores no-speech errors while listening", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        onTranscript: vi.fn(),
        onError,
      }),
    );

    await startSpeech(() => result.current.start());

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

  it("uses cached finalized chunks for results before resultIndex", async () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() =>
      useSpeechToText({
        getBaseText: () => "",
        onTranscript,
      }),
    );

    await startSpeech(() => result.current.start());

    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("hello world");
    });
    act(() => {
      MockSpeechRecognition.lastInstance?.emitResult("more text", true, 1);
    });

    expect(onTranscript).toHaveBeenLastCalledWith("hello world more text");
  });
});
