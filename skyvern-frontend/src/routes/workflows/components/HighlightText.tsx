interface HighlightTextProps {
  text: string;
  query?: string;
}

function HighlightText({ text, query }: HighlightTextProps) {
  if (!query || !query.trim()) {
    return <>{text}</>;
  }

  const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const q = query.trim();
  const regex = new RegExp(`(${escapeRegExp(q)})`, "gi");
  const parts = text.split(regex);
  const lowerQ = q.toLowerCase();

  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === lowerQ ? (
          <span key={i} className="rounded bg-blue-500/30 px-0.5 text-blue-400">
            {part}
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

export { HighlightText };
