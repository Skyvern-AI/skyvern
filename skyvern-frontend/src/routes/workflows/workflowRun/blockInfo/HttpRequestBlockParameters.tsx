import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

type Props = {
  method: string;
  url: string | null;
  headers: Record<string, string> | null;
  body: Record<string, unknown> | null;
  files: Record<string, string> | null;
  timeout: number;
  followRedirects: boolean;
  downloadFilename: string | null;
  saveResponseAsFile: boolean;
};

function HttpRequestBlockParameters({
  method,
  url,
  headers,
  body,
  files,
  timeout,
  followRedirects,
  downloadFilename,
  saveResponseAsFile,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Method</h1>
        </div>
        <Input value={method} readOnly />
      </div>
      {url ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">URL</h1>
          </div>
          <AutoResizingTextarea value={url} readOnly />
        </div>
      ) : null}
      {headers && Object.keys(headers).length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Headers</h1>
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={JSON.stringify(headers, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      ) : null}
      {body && Object.keys(body).length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Body</h1>
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={JSON.stringify(body, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      ) : null}
      {files && Object.keys(files).length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Files</h1>
            <h2 className="text-base text-slate-400">
              File fields and their paths/URLs
            </h2>
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={JSON.stringify(files, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      ) : null}
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Timeout</h1>
          <h2 className="text-base text-slate-400">In seconds</h2>
        </div>
        <Input value={timeout.toString()} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Follow Redirects</h1>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={followRedirects} disabled />
          <span className="text-sm text-slate-400">
            {followRedirects ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>
      {downloadFilename ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Download Filename</h1>
          </div>
          <Input value={downloadFilename} readOnly />
        </div>
      ) : null}
      {saveResponseAsFile ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Save Response as File</h1>
          </div>
          <div className="flex w-full items-center gap-3">
            <Switch checked={true} disabled />
            <span className="text-sm text-slate-400">Enabled</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { HttpRequestBlockParameters };
