declare module "@novnc/novnc/lib/rfb.js" {
  export interface RfbEvent {
    detail: {
      clean: boolean;
      reason: string;
      error: {
        message: string;
      };
      message: string;
    };
  }

  export interface RfbDisplay {
    autoscale(): void;
    _scale: number;
  }

  export interface RFBOptions {
    credentials?: { username?: string; password?: string };
    clipViewport?: boolean;
    scaleViewport?: boolean;
    shared?: boolean;
    resizeSession?: boolean;
    viewOnly?: boolean;
    [key: string]: unknown;
  }

  export default class RFB {
    _display: RfbDisplay;
    resizeSession: boolean;
    scaleViewport: boolean;
    // 0–9; JPEG quality for Tight-encoded regions (default 6).
    qualityLevel: number;
    // 0–9; zlib compression for stream data (default 2).
    compressionLevel: number;
    constructor(target: HTMLElement, url: string, options?: RFBOptions);

    addEventListener(event: string, listener: (e: RfbEvent) => void): void;
    removeEventListener(event: string, listener: (e: RfbEvent) => void): void;
    clipboardPasteFrom(text: string): void;
    disconnect(): void;
    sendKey(keysym: number, code: string, down?: boolean): void;
    viewportChange(): void;
  }
}

declare module "@novnc/novnc/lib/input/gesturehandler.js" {
  interface TrackedTouch {
    id: number;
  }

  export default class GestureHandler {
    _ignored: number[];
    _tracked: TrackedTouch[];

    attach(target: HTMLElement): void;
    _touchStart(id: number, x: number, y: number): void;
    _touchEnd(id: number, x: number, y: number): void;
  }
}
