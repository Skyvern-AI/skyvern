interface BrowserSession {
  browser_address: string | null;
  browser_session_id: string;
  completed_at: string | null;
  recordings: Recording[];
  runnable_id: string | null;
  runnable_type: string | null;
  started_at: string | null;
  timeout: number | null;
  vnc_streaming_supported: boolean;
}

interface Recording {
  url: string;
  checksum: string;
  filename: string;
  modified_at: string;
}

export { type BrowserSession, type Recording };
