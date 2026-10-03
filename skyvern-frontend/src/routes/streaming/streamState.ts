// "stopped" means the stream gave up on its own (reconnects exhausted or the
// session/run reached a terminal status); only a remount recovers it.
export type StreamState = "connecting" | "live" | "stopped";

export type StreamStateChangeHandler = (
  state: StreamState,
  browserSessionId: string | null,
) => void;
