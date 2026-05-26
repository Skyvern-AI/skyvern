import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import { PlusIcon } from "@radix-ui/react-icons";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Props = {
  onAdd: (headers: Record<string, string>) => void;
  children: React.ReactNode;
};

const commonHeaders = [
  {
    name: "Content-Type",
    value: "application/json",
    description: "JSON content",
  },
  {
    name: "Content-Type",
    value: "application/x-www-form-urlencoded",
    description: "Form data",
  },
  {
    name: "Authorization",
    value: "Bearer YOUR_TOKEN",
    description: "Bearer token auth",
  },
  {
    name: "Authorization",
    value: "Basic YOUR_CREDENTIALS",
    description: "Basic auth",
  },
  { name: "User-Agent", value: "Skyvern/1.0", description: "User agent" },
  {
    name: "Accept",
    value: "application/json",
    description: "Accept JSON response",
  },
  { name: "Accept", value: "*/*", description: "Accept any response" },
  { name: "X-API-Key", value: "YOUR_API_KEY", description: "API key header" },
  { name: "Cache-Control", value: "no-cache", description: "No cache" },
  {
    name: "Referer",
    value: "https://example.com",
    description: "Referer header",
  },
];

export function QuickHeadersDialog({ onAdd, children }: Props) {
  const [open, setOpen] = useState(false);
  const [selectedHeaders, setSelectedHeaders] = useState<
    Record<string, string>
  >({});
  const [customKey, setCustomKey] = useState("");
  const [customValue, setCustomValue] = useState("");

  const handleAddCustomHeader = () => {
    if (customKey.trim() && customValue.trim()) {
      setSelectedHeaders((prev) => ({
        ...prev,
        [customKey.trim()]: customValue.trim(),
      }));
      setCustomKey("");
      setCustomValue("");
    }
  };

  const handleToggleHeader = (name: string, value: string) => {
    setSelectedHeaders((prev) => {
      const newHeaders = { ...prev };
      if (newHeaders[name] === value) {
        delete newHeaders[name];
      } else {
        newHeaders[name] = value;
      }
      return newHeaders;
    });
  };

  const handleAddHeaders = () => {
    if (Object.keys(selectedHeaders).length > 0) {
      onAdd(selectedHeaders);
      setSelectedHeaders({});
      setOpen(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="max-h-[80vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <PlusIcon className="h-5 w-5" />
            Add Common Headers
          </DialogTitle>
          <DialogDescription>
            Quickly add common HTTP headers to your request.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Common Headers */}
          <div>
            <h4 className="mb-3 text-sm font-medium">Common Headers</h4>
            <div className="grid grid-cols-1 gap-2">
              {commonHeaders.map((header, index) => {
                const isSelected =
                  selectedHeaders[header.name] === header.value;
                return (
                  <div
                    key={index}
                    className={`cursor-pointer rounded-lg border p-3 transition-colors hover:bg-slate-50 dark:hover:bg-slate-800 ${
                      isSelected
                        ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
                        : ""
                    }`}
                    onClick={() =>
                      handleToggleHeader(header.name, header.value)
                    }
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Badge variant="outline" className="font-mono text-xs">
                          {header.name}
                        </Badge>
                        <span className="text-sm text-slate-600 dark:text-slate-400">
                          {header.value}
                        </span>
                      </div>
                      {isSelected && (
                        <Badge variant="default" className="text-xs">
                          Selected
                        </Badge>
                      )}
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      {header.description}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Custom Header */}
          <div>
            <h4 className="mb-3 text-sm font-medium">Custom Header</h4>
            <div className="flex gap-2">
              <div className="flex-1">
                <Label htmlFor="custom-key" className="text-xs">
                  Header Name
                </Label>
                <Input
                  id="custom-key"
                  placeholder="X-Custom-Header"
                  value={customKey}
                  onChange={(e) => setCustomKey(e.target.value)}
                  className="text-sm"
                />
              </div>
              <div className="flex-1">
                <Label htmlFor="custom-value" className="text-xs">
                  Header Value
                </Label>
                <Input
                  id="custom-value"
                  placeholder="custom-value"
                  value={customValue}
                  onChange={(e) => setCustomValue(e.target.value)}
                  className="text-sm"
                />
              </div>
              <div className="flex items-end">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleAddCustomHeader}
                  disabled={!customKey.trim() || !customValue.trim()}
                >
                  <PlusIcon className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </div>

          {/* Selected Headers Preview */}
          {Object.keys(selectedHeaders).length > 0 && (
            <div>
              <h4 className="mb-3 text-sm font-medium">
                Selected Headers ({Object.keys(selectedHeaders).length})
              </h4>
              <div className="rounded-lg border bg-slate-50 p-3 dark:bg-slate-800">
                <pre className="text-xs text-slate-600 dark:text-slate-400">
                  {JSON.stringify(selectedHeaders, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleAddHeaders}
            disabled={Object.keys(selectedHeaders).length === 0}
          >
            Add Headers ({Object.keys(selectedHeaders).length})
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
