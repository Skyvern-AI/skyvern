import fetchToCurl from "fetch-to-curl";

export interface ApiCommandOptions {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: unknown;
}

function toPowershellHashtable(value: unknown, indent = 0): string {
  const indentation = "  ".repeat(indent);
  if (value === null) {
    return `${indentation}$null`;
  }
  if (typeof value === "string") {
    return `${indentation}"${value}"`;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return `${indentation}${value}`;
  }
  if (Array.isArray(value)) {
    const items = value
      .map((item) => toPowershellHashtable(item, indent + 1))
      .join("\n");
    return `${indentation}@(\n${items}\n${indentation})`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => {
        const formatted =
          typeof v === "object" && v !== null
            ? `@{\n${toPowershellHashtable(v, indent + 1)}\n${indentation}}`
            : toPowershellHashtable(v, 0).trim();
        return `${indentation}"${k}" = ${formatted}`;
      })
      .join("\n");
    return entries;
  }
  return `${indentation}"${String(value)}"`;
}

function generateApiCommands(options: ApiCommandOptions): {
  curl: string;
  powershell: string;
} {
  const curl = fetchToCurl(options);

  const headerLines = Object.entries(options.headers ?? {})
    .map(([k, v]) => `    "${k}" = "${v}"`)
    .join("\n");

  let bodySection = "";
  if (typeof options.body !== "undefined") {
    const bodyLines = toPowershellHashtable(options.body, 2);
    bodySection = `\n  Body    = (ConvertTo-Json @{\n${bodyLines}\n  })`;
  }

  const powershell = `$Params = @{\n  Uri     = "${options.url}"\n  Method  = "${options.method.toUpperCase()}"\n  Headers = @{\n${headerLines}\n  }${bodySection}\n}\nInvoke-RestMethod @Params`;

  return { curl, powershell };
}

export { generateApiCommands };
