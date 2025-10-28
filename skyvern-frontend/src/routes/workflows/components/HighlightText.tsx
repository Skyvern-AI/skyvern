interface HighlightTextProps {
  text: string;
  query?: string;
}

function HighlightText({ text, query }: HighlightTextProps) {
  if (!query || !query.trim()) {
    return <>{text}</>;
  }

  const parts = text.split(new RegExp(`(${query})`, "gi"));

  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === query.toLowerCase() ? (
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
