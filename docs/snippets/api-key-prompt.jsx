function dedent(text) {
  if (typeof text !== "string") return String(text);
  var lines = text.split("\n");
  var indents = [];
  for (var i = 1; i < lines.length; i++) {
    if (lines[i].trim().length > 0) {
      var match = lines[i].match(/^(\s*)/);
      indents.push(match ? match[1].length : 0);
    }
  }
  var min = indents.length > 0 ? Math.min.apply(null, indents) : 0;
  if (min === 0) return text;
  return lines
    .map(function (line, i) {
      return i === 0 ? line : line.slice(min);
    })
    .join("\n");
}

export const ApiKeyPrompt = ({ children }) => {
  const [apiKey, setApiKey] = React.useState("");
  const [copied, setCopied] = React.useState(false);

  const raw = dedent(children);
  const content = apiKey
    ? raw.replace(/PASTE_YOUR_API_KEY_HERE/g, apiKey)
    : raw;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div>
      <input
        type="text"
        placeholder="Paste your Skyvern API key"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        spellCheck={false}
        autoComplete="off"
        style={{
          width: "100%",
          padding: "10px 14px",
          marginBottom: "12px",
          borderRadius: "8px",
          border: "1px solid var(--border, #d1d5db)",
          fontSize: "14px",
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          backgroundColor: "var(--background, transparent)",
          color: "inherit",
          outline: "none",
          boxSizing: "border-box",
        }}
      />
      <div
        style={{
          position: "relative",
          borderRadius: "8px",
          overflow: "hidden",
        }}
      >
        <button
          onClick={handleCopy}
          style={{
            position: "absolute",
            top: "10px",
            right: "10px",
            padding: "4px 12px",
            fontSize: "12px",
            borderRadius: "6px",
            border: "none",
            backgroundColor: "rgba(255, 255, 255, 0.1)",
            color: "#a1a1aa",
            cursor: "pointer",
            zIndex: 1,
            transition: "color 0.15s ease",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.color = "#e5e7eb")}
          onMouseLeave={(e) => (e.currentTarget.style.color = "#a1a1aa")}
        >
          {copied ? "Copied!" : "Copy"}
        </button>
        <pre
          style={{
            margin: 0,
            padding: "16px",
            overflowX: "auto",
            fontSize: "13px",
            lineHeight: "1.7",
            backgroundColor: "#0d1117",
            color: "#e6edf3",
          }}
        >
          <code>{content}</code>
        </pre>
      </div>
    </div>
  );
};
