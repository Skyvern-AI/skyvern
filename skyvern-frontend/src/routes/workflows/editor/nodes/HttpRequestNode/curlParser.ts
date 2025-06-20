interface ParsedCurlCommand {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: string;
}

export function parseCurlCommand(curlCommand: string): ParsedCurlCommand {
  if (!curlCommand || !curlCommand.trim()) {
    throw new Error("Empty curl command");
  }

  const command = curlCommand.trim();
  
  // Check if it starts with curl
  if (!command.toLowerCase().startsWith("curl")) {
    throw new Error("Command must start with 'curl'");
  }

  const result: ParsedCurlCommand = {
    method: "GET",
    url: "",
    headers: {},
    body: "",
  };

  // Remove 'curl' and split into tokens
  const cleanCommand = command.substring(4).trim();
  const tokens = tokenizeCurlCommand(cleanCommand);
  
  let i = 0;
  while (i < tokens.length) {
    const token = tokens[i];
    if (!token) {
      i++;
      continue;
    }

    if (token === "-X" || token === "--request") {
      const nextToken = tokens[i + 1];
      if (nextToken) {
        result.method = nextToken.toUpperCase();
        i += 2;
      } else {
        throw new Error("Method flag requires a value");
      }
    } else if (token === "-H" || token === "--header") {
      const header = tokens[i + 1];
      if (header) {
        const colonIndex = header.indexOf(":");
        if (colonIndex > 0) {
          const key = header.substring(0, colonIndex).trim();
          const value = header.substring(colonIndex + 1).trim();
          result.headers[key] = value;
        }
        i += 2;
      } else {
        throw new Error("Header flag requires a value");
      }
    } else if (token === "-d" || token === "--data" || token === "--data-raw") {
      const data = tokens[i + 1];
      if (data) {
        result.body = data;
        i += 2;
      } else {
        throw new Error("Data flag requires a value");
      }
    } else if (token === "-u" || token === "--user") {
      const auth = tokens[i + 1];
      if (auth) {
        // Convert -u user:pass to Authorization header
        result.headers["Authorization"] = `Basic ${btoa(auth)}`;
        i += 2;
      } else {
        throw new Error("User flag requires a value");
      }
    } else if (token.startsWith("-")) {
      // Skip other flags we don't handle
      i += 2;
    } else if (!result.url && isUrl(token)) {
      result.url = cleanQuotes(token);
      i++;
    } else {
      i++;
    }
  }

  if (!result.url) {
    throw new Error("No URL found in curl command");
  }

  // If method is POST/PUT/PATCH and no content-type is set, default to JSON
  if (["POST", "PUT", "PATCH"].includes(result.method) && result.body && !result.headers["Content-Type"]) {
    if (result.body.trim().startsWith("{") || result.body.trim().startsWith("[")) {
      result.headers["Content-Type"] = "application/json";
    }
  }

  return result;
}

function tokenizeCurlCommand(command: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let inQuotes = false;
  let quoteChar = "";
  let escapeNext = false;

  for (let i = 0; i < command.length; i++) {
    const char = command[i];

    if (escapeNext) {
      current += char;
      escapeNext = false;
      continue;
    }

    if (char === "\\") {
      escapeNext = true;
      continue;
    }

    if (!inQuotes && (char === "'" || char === '"')) {
      inQuotes = true;
      quoteChar = char;
    } else if (inQuotes && char === quoteChar) {
      inQuotes = false;
      quoteChar = "";
    } else if (!inQuotes && char === " ") {
      if (current) {
        tokens.push(current);
        current = "";
      }
    } else {
      current += char;
    }
  }

  if (current) {
    tokens.push(current);
  }

  return tokens;
}

function cleanQuotes(str: string): string {
  if ((str.startsWith("'") && str.endsWith("'")) || 
      (str.startsWith('"') && str.endsWith('"'))) {
    return str.slice(1, -1);
  }
  return str;
}

function isUrl(str: string): boolean {
  const cleaned = cleanQuotes(str);
  return cleaned.startsWith("http://") || 
         cleaned.startsWith("https://") || 
         cleaned.startsWith("/") ||
         cleaned.includes(".");
}

// Example usage:
// parseCurlCommand("curl -X POST 'https://api.example.com' -H 'Content-Type: application/json' -d '{\"key\":\"value\"}'")
// Returns: {
//   method: "POST",
//   url: "https://api.example.com",
//   headers: { "Content-Type": "application/json" },
//   body: "{\"key\":\"value\"}"
// }