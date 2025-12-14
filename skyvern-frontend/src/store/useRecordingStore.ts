import { create } from "zustand";

/**
 * example: {
 *  'targetInfo': {
 *    'targetId': '8B698E27F1F32372718DA73DCA0C5944',
 *    'type': 'page',
 *    'title': 'New Tab',
 *    'url': 'chrome://newtab/',
 *    'attached': True,
 *    'canAccessOpener': False,
 *    'browserContextId': 'FD13D5C556E681BB49AEED0AB2CA1972',
 * }
 */
export interface ExfiltratedEventCdpParams {
  targetInfo: {
    attached?: boolean;
    browserContextId?: string;
    canAccessOpener?: boolean;
    targetId?: string;
    title?: string;
    type?: string;
    url?: string;
  };
}

export interface ExfiltratedEventConsoleParams {
  type: string;
  url: string;
  timestamp: number;
  target: {
    className?: string;
    id?: string;
    innerText?: string;
    tagName?: string;
    text: string[];
    value?: string;
  };
  inputValue?: string;
  mousePosition: {
    xa: number | null;
    ya: number | null;
    xp: number | null;
    yp: number | null;
  };
  key?: string;
  code?: string;
  activeElement: {
    tagName?: string;
    id?: string;
    className?: string;
    boundingRect?: {
      x: number;
      y: number;
      width: number;
      height: number;
      top: number;
      right: number;
      bottom: number;
      left: number;
    } | null;
    scroll?: {
      scrollTop: number;
      scrollLeft: number;
      scrollHeight: number;
      scrollWidth: number;
      clientHeight: number;
      clientWidth: number;
    } | null;
  };
  window: {
    width: number;
    height: number;
    scrollX: number;
    scrollY: number;
  };
}

export interface MessageInExfiltratedCdpEvent {
  kind: "exfiltrated-event";
  event_name: string;
  params: ExfiltratedEventCdpParams;
  source: "cdp";
  timestamp: number;
}

export interface MessageInExfiltratedConsoleEvent {
  kind: "exfiltrated-event";
  event_name: string;
  params: ExfiltratedEventConsoleParams;
  source: "console";
  timestamp: number;
}

export type MessageInExfiltratedEvent =
  | MessageInExfiltratedCdpEvent
  | MessageInExfiltratedConsoleEvent;

/**
 * Number of events per compressed chunk.
 */
export const CHUNK_SIZE = 1000 as const;

interface RecordingStore {
  /**
   * Compressed chunks of recorded events (base64 gzip).
   * Each chunk contains up to CHUNK_SIZE events.
   */
  compressedChunks: string[];
  /**
   * The number of events to show the user. This elides noisy events, like
   * `mousemove`.
   */
  exposedEventCount: number;
  /**
   * Buffer of events not yet compressed into a chunk.
   */
  pendingEvents: MessageInExfiltratedEvent[];
  /**
   * Whether a compression operation is currently in progress.
   */
  isCompressing: boolean;
  /**
   * Whether the user is currently in browser recording mode.
   */
  isRecording: boolean;
  /**
   * Add a new recorded event. Triggers async compression when buffer is full.
   */
  add: (event: MessageInExfiltratedEvent) => void;
  /**
   * Clear all recorded events and compressed chunks.
   */
  clear: () => void;
  /**
   * Reset the recording store (clear events and set isRecording to false).
   */
  reset: () => void;
  /**
   * Set whether the user is in browser recording mode.
   */
  setIsRecording: (isRecording: boolean) => void;
  /**
   * Flush any pending events into a compressed chunk.
   * Call this before consuming the data.
   */
  flush: () => Promise<void>;
  /**
   * Get all compressed chunks (after flushing pending events).
   */
  getCompressedChunks: () => Promise<string[]>;
  /**
   * Get the total number of events (compressed + pending).
   */
  getEventCount: () => number;
}

/**
 * compresses a JSON string using the Gzip algorithm and returns the result
 * as a Base64 encoded string
 */
async function compressEventsToB64(jsonString: string): Promise<string> {
  // 1. Convert the string to a Uint8Array (a byte array).
  const encoder = new TextEncoder();
  const uint8Array = encoder.encode(jsonString);

  // 2. Create a ReadableStream from the byte array.
  const readableStream = new ReadableStream({
    start(controller) {
      controller.enqueue(uint8Array);
      controller.close();
    },
  });

  // 3. Pipe the data through the Gzip compression stream.
  const compressedStream = readableStream.pipeThrough(
    new CompressionStream("gzip"), // Use 'gzip' for standard network transport
  );

  // 4. Read the entire compressed stream back into a single ArrayBuffer.
  // The Response object provides an easy way to convert streams into a single buffer.
  const compressedBuffer = await new Response(compressedStream).arrayBuffer();

  // 5. Convert the ArrayBuffer (binary data) to a Base64 string for transport.
  // Base64 is used to safely transmit binary data over text-based protocols (like JSON).
  const bytes = new Uint8Array(compressedBuffer);
  let binary = "";

  // Convert Uint8Array to a raw binary string (this is needed for btoa)
  for (let i = 0; i < bytes.length; i++) {
    const nextByte = bytes[i];

    if (nextByte === undefined) {
      continue;
    }

    binary += String.fromCharCode(nextByte);
  }

  // Convert the raw binary string to Base64
  return btoa(binary);
}

const isExposedEvent = (event: MessageInExfiltratedEvent): boolean => {
  const exposedConsoleEventTypes = new Set(["focus", "click", "keypress"]);

  if (event.source === "console") {
    if (exposedConsoleEventTypes.has(event.params.type)) {
      return true;
    }
  }

  if (event.source === "cdp") {
    return true;
  }

  return false;
};

export const useRecordingStore = create<RecordingStore>((set, get) => ({
  compressedChunks: [],
  exposedEventCount: 0,
  pendingEvents: [],
  isCompressing: false,
  isRecording: false,

  add: (event) => {
    const state = get();
    const newPendingEvents = [...state.pendingEvents, event];

    if (isExposedEvent(event)) {
      set({ exposedEventCount: state.exposedEventCount + 1 });
    }

    if (newPendingEvents.length >= CHUNK_SIZE && !state.isCompressing) {
      const eventsToCompress = newPendingEvents.slice(0, CHUNK_SIZE);
      const remainingEvents = newPendingEvents.slice(CHUNK_SIZE);

      set({ pendingEvents: remainingEvents, isCompressing: true });

      // compress asynchronously
      queueMicrotask(async () => {
        try {
          const jsonString = JSON.stringify(eventsToCompress);
          const compressed = await compressEventsToB64(jsonString);
          const currentState = get();
          set({
            compressedChunks: [...currentState.compressedChunks, compressed],
            isCompressing: false,
          });
        } catch (error) {
          console.error("Failed to compress events chunk:", error);

          // on error, put events back into pending
          const currentState = get();
          set({
            pendingEvents: [...eventsToCompress, ...currentState.pendingEvents],
            isCompressing: false,
          });
        }
      });
    } else {
      set({ pendingEvents: newPendingEvents });
    }
  },

  clear: () => set({ compressedChunks: [], pendingEvents: [] }),

  reset: () =>
    set({
      compressedChunks: [],
      exposedEventCount: 0,
      pendingEvents: [],
      isCompressing: false,
      isRecording: false,
    }),

  setIsRecording: (isRecording) => {
    const state = get();
    // clear events on rising edge
    if (!state.isRecording && isRecording) {
      get().clear();
    }
    set({ isRecording });
  },

  flush: async () => {
    // Wait for any in-progress compression to complete
    while (get().isCompressing) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }

    const pending = get().pendingEvents;
    if (pending.length === 0) {
      return;
    }

    set({ isCompressing: true });

    try {
      const jsonString = JSON.stringify(pending);
      const compressed = await compressEventsToB64(jsonString);
      const currentState = get();
      set({
        compressedChunks: [...currentState.compressedChunks, compressed],
        pendingEvents: [],
        isCompressing: false,
      });
    } catch (error) {
      console.error("Failed to flush pending events:", error);
      set({ isCompressing: false });
      throw error;
    }
  },

  getCompressedChunks: async () => {
    await get().flush();
    return get().compressedChunks;
  },

  getEventCount: () => {
    const state = get();
    return (
      state.compressedChunks.length * CHUNK_SIZE + state.pendingEvents.length
    );
  },
}));
