// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isBrowserFetchableUrl } from "./fileUploadLink";

const { postMock, toastMock } = vi.hoisted(() => ({
  postMock: vi.fn().mockResolvedValue({ data: {} }),
  toastMock: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ post: postMock })),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("./ui/use-toast", () => ({ toast: toastMock }));

import { FileUpload } from "./FileUpload";

const MB = 1024 * 1024;

function fileWithSize(size: number) {
  const file = new File([""], "upload.csv", { type: "text/csv" });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function renderFileUpload() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <FileUpload value={null} onChange={() => undefined} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("upload download links", () => {
  it("links an address the browser can fetch and nothing else", () => {
    expect(
      isBrowserFetchableUrl("https://files.example.com/o_1/report.csv"),
    ).toBe(true);
    expect(isBrowserFetchableUrl("http://localhost:8000/files/file_1")).toBe(
      true,
    );
    // Local storage answers with the server's own path; an https page cannot read it.
    expect(
      isBrowserFetchableUrl(
        "file:///srv/artifacts/local/o_1/2026-09-12/file_1_report.csv",
      ),
    ).toBe(false);
  });
});

describe("FileUpload size limit", () => {
  it("accepts 25 MB files and rejects files over 30 MB", async () => {
    const accepted = renderFileUpload();
    fireEvent.change(accepted.container.querySelector('input[type="file"]')!, {
      target: { files: [fileWithSize(25 * MB)] },
    });
    await waitFor(() => expect(postMock).toHaveBeenCalledOnce());
    accepted.unmount();

    const rejected = renderFileUpload();
    fireEvent.change(rejected.container.querySelector('input[type="file"]')!, {
      target: { files: [fileWithSize(30 * MB + 1)] },
    });

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        description: expect.stringContaining("30MB limit"),
      }),
    );
    expect(postMock).toHaveBeenCalledOnce();
  });
});
