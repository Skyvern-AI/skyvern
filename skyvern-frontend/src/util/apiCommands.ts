import fetchToCurl from "fetch-to-curl";

export type ApiRequest = {
  method: string;
  url: string;
  body?: unknown;
  headers: Record<string, string>;
};

export function getCurlCommand(request: ApiRequest): string {
  return fetchToCurl(request);
}

export function getPowerShellCommand(request: ApiRequest): string {
  const { method, url, headers, body } = request;
  const headerLines = Object.entries(headers)
    .map(([key, value]) => `    '${key}' = '${value}'`)
    .join("\n");
  const bodyJson = body ? JSON.stringify(body, null, 2) : undefined;
  const bodyLine = bodyJson
    ? `  Body = '${bodyJson.replace(/'/g, "''")}'\n`
    : "";
  return (
    `$Params = @{\n` +
    `  Uri = '${url}'\n` +
    `  Method = '${method}'\n` +
    `  Headers = @{\n${headerLines}\n  }\n` +
    bodyLine +
    `}\n` +
    `Invoke-RestMethod @Params`
  );
}
