type BrowserSessionExtension = "ad-blocker" | "captcha-solver";
type BrowserSessionType = "msedge" | "chrome";

interface BrowserSession {
  browser_address: string | null;
  browser_session_id: string;
  completed_at: string | null;
  downloaded_files: DownloadedFile[] | null;
  recordings: Recording[];
  runnable_id: string | null;
  runnable_type: string | null;
  started_at: string | null;
  status: string;
  timeout: number | null;
  extensions?: BrowserSessionExtension[] | null;
  browser_type?: BrowserSessionType | null;
  vnc_streaming_supported: boolean;
}

interface Recording {
  url: string;
  checksum: string;
  filename: string;
  modified_at: string;
}

interface DownloadedFile {
  url: string;
  checksum: string | null;
  filename: string | null;
  modified_at: string | null;
}

export {
  type BrowserSession,
  type BrowserSessionExtension,
  type BrowserSessionType,
  type DownloadedFile,
  type Recording,
};
