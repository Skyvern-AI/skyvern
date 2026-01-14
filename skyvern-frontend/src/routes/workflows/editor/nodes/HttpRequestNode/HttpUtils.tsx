import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  CopyIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  CheckCircledIcon,
} from "@radix-ui/react-icons";
import { useState } from "react";
import { toast } from "@/components/ui/use-toast";
import { cn } from "@/util/utils";

// HTTP Method Badge Component
export function MethodBadge({
  method,
  className,
}: {
  method: string;
  className?: string;
}) {
  const getMethodStyle = (method: string) => {
    switch (method.toUpperCase()) {
      case "GET":
        return "bg-green-100 text-green-800 border-green-300 dark:bg-green-900/20 dark:text-green-400 dark:border-green-800";
      case "POST":
        return "bg-blue-100 text-blue-800 border-blue-300 dark:bg-blue-900/20 dark:text-blue-400 dark:border-blue-800";
      case "PUT":
        return "bg-yellow-100 text-yellow-800 border-yellow-300 dark:bg-yellow-900/20 dark:text-yellow-400 dark:border-yellow-800";
      case "DELETE":
        return "bg-red-100 text-red-800 border-red-300 dark:bg-red-900/20 dark:text-red-400 dark:border-red-800";
      case "PATCH":
        return "bg-purple-100 text-purple-800 border-purple-300 dark:bg-purple-900/20 dark:text-purple-400 dark:border-purple-800";
      case "HEAD":
        return "bg-gray-100 text-gray-800 border-gray-300 dark:bg-gray-900/20 dark:text-gray-400 dark:border-gray-800";
      case "OPTIONS":
        return "bg-cyan-100 text-cyan-800 border-cyan-300 dark:bg-cyan-900/20 dark:text-cyan-400 dark:border-cyan-800";
      default:
        return "bg-slate-100 text-slate-800 border-slate-300 dark:bg-slate-900/20 dark:text-slate-400 dark:border-slate-800";
    }
  };

  return (
    <Badge
      variant="outline"
      className={cn(
        "border font-mono text-xs font-bold",
        getMethodStyle(method),
        className,
      )}
    >
      {method}
    </Badge>
  );
}

// URL Validation Component
export function UrlValidator({ url }: { url: string }) {
  const isValidUrl = (urlString: string) => {
    if (!urlString.trim()) return { valid: false, message: "URL is required" };

    try {
      const url = new URL(urlString);
      if (!["http:", "https:"].includes(url.protocol)) {
        return { valid: false, message: "URL must use HTTP or HTTPS protocol" };
      }
      return { valid: true, message: "Valid URL" };
    } catch {
      return { valid: false, message: "Invalid URL format" };
    }
  };

  const validation = isValidUrl(url);

  if (!url.trim()) return null;

  return (
    <div
      className={cn(
        "flex items-center gap-1 text-xs",
        validation.valid
          ? "text-green-600 dark:text-green-400"
          : "text-red-600 dark:text-red-400",
      )}
    >
      {validation.valid ? (
        <CheckCircledIcon className="h-3 w-3" />
      ) : (
        <ExclamationTriangleIcon className="h-3 w-3" />
      )}
      <span>{validation.message}</span>
    </div>
  );
}

// Copy to Curl Component
export function CopyToCurlButton({
  method,
  url,
  headers,
  body,
  className,
}: {
  method: string;
  url: string;
  headers: string;
  body: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  const generateCurlCommand = () => {
    let curl = `curl -X ${method.toUpperCase()}`;

    if (url) {
      curl += ` "${url}"`;
    }

    // Parse and add headers
    try {
      const parsedHeaders = JSON.parse(headers || "{}");
      Object.entries(parsedHeaders).forEach(([key, value]) => {
        curl += ` \\\n  -H "${key}: ${value}"`;
      });
    } catch (error) {
      // If headers can't be parsed, skip them
    }

    // Add body for non-GET requests
    if (["POST", "PUT", "PATCH"].includes(method.toUpperCase()) && body) {
      try {
        const parsedBody = JSON.parse(body);
        curl += ` \\\n  -d '${JSON.stringify(parsedBody)}'`;
      } catch (error) {
        // If body can't be parsed, add it as-is
        curl += ` \\\n  -d '${body}'`;
      }
    }

    return curl;
  };

  const handleCopy = async () => {
    try {
      const curlCommand = generateCurlCommand();
      await navigator.clipboard.writeText(curlCommand);
      setCopied(true);
      toast({
        title: "Copied!",
        description: "cURL command copied to clipboard",
      });
      setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to copy cURL command",
        variant: "destructive",
      });
    }
  };

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={handleCopy}
      className={cn("h-8 px-2", className)}
      disabled={!url}
    >
      {copied ? (
        <CheckIcon className="mr-1 h-4 w-4" />
      ) : (
        <CopyIcon className="mr-1 h-4 w-4" />
      )}
      {copied ? "Copied!" : "Copy cURL"}
    </Button>
  );
}

// Request Preview Component
export function RequestPreview({
  method,
  url,
  headers,
  body,
  files,
}: {
  method: string;
  url: string;
  headers: string;
  body: string;
  files?: string;
}) {
  const [expanded, setExpanded] = useState(false);

  const hasContent = method && url;

  if (!hasContent) return null;

  const hasFiles = files && files.trim() && files !== "{}";

  return (
    <div className="rounded-md border bg-slate-50 p-3 dark:bg-slate-900/50">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <MethodBadge method={method} />
          <span className="font-mono text-sm text-slate-600 dark:text-slate-400">
            {url || "No URL specified"}
          </span>
          {hasFiles && (
            <Badge variant="outline" className="text-xs">
              Files
            </Badge>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setExpanded(!expanded)}
          className="h-6 text-xs"
        >
          {expanded ? "Hide" : "Show"} Details
        </Button>
      </div>

      {expanded && (
        <div className="mt-3 space-y-2">
          {/* Headers */}
          <div>
            <div className="mb-1 text-xs font-medium">Headers:</div>
            <pre className="overflow-x-auto rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
              {headers || "{}"}
            </pre>
          </div>

          {/* Body (only for POST, PUT, PATCH) */}
          {["POST", "PUT", "PATCH"].includes(method.toUpperCase()) && (
            <div>
              <div className="mb-1 text-xs font-medium">Body:</div>
              <pre className="overflow-x-auto rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                {body || "{}"}
              </pre>
            </div>
          )}

          {/* Files (only for POST, PUT, PATCH) */}
          {["POST", "PUT", "PATCH"].includes(method.toUpperCase()) &&
            hasFiles && (
              <div>
                <div className="mb-1 text-xs font-medium">Files:</div>
                <pre className="overflow-x-auto rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                  {files || "{}"}
                </pre>
              </div>
            )}
        </div>
      )}
    </div>
  );
}
