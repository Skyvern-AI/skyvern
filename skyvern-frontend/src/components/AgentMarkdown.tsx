import { memo, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";

// Agent prose (a Task V3 turn's text, a v1 action's reasoning, an observer thought) is markdown,
// and it is untrusted: the model writes it while reading an arbitrary page. Two constraints follow.
//
// It has to stay phrasing content. Callers render it inside a <button> or a single truncated line,
// where a block element breaks the line box and an anchor or form control is invalid nesting the
// browser may reparent. So every construct collapses to inline content and links lose their href.
//
// No `skipHtml`: react-markdown *deletes* raw HTML rather than escaping it, so reasoning that names
// a tag ("clicked the <button> inside the <form>") would silently lose those words. Without
// rehype-raw the same input is escaped and rendered as the literal text the model wrote, which is
// both lossless and inert.
const passthrough = ({ children }: { children?: ReactNode }) => <>{children}</>;

const sharedComponents: Components = {
  ul: passthrough,
  ol: passthrough,
  pre: passthrough,
  li: ({ children }) => <span>{children} </span>,
  a: ({ children }) => <span>{children}</span>,
  br: () => <> </>,
  // Nothing may render to nothing: prose that is only an image or only a rule would otherwise leave
  // the row blank again, which is the bug this renderer exists to fix.
  hr: () => <span> — </span>,
  img: ({ alt }) => <>{alt}</>,
  code: ({ children }) => (
    <code className="rounded bg-slate-500/15 px-1 font-mono text-[0.92em]">
      {children}
    </code>
  ),
};

const HEADING_KEYS = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;

function withKeys(
  keys: ReadonlyArray<string>,
  component: Components[keyof Components],
): Components {
  return Object.fromEntries(keys.map((key) => [key, component]));
}

const inlineComponents: Components = {
  ...sharedComponents,
  ...withKeys([...HEADING_KEYS, "blockquote", "p"], passthrough),
};

// A span that displays as a block: paragraph separation without leaving phrasing content.
const asBlock = ({ children }: { children?: ReactNode }) => (
  <span className="block [&:not(:first-child)]:mt-2">{children}</span>
);

const blockComponents: Components = {
  ...sharedComponents,
  ...withKeys([...HEADING_KEYS, "blockquote", "p"], asBlock),
};

// Some valid markdown renders to nothing at all - an image with an empty alt, a bare link
// reference definition - and a caller cannot tell from the source string, which is non-empty. Rather
// than enumerate those constructs, measure the output: if it carries no text, show `fallback`
// instead. Without this a timeline row is back to a bare icon and index, the bug this all exists to
// fix. The default fallback is the source itself, so the reader still sees what the model wrote.
function Rendered({
  text,
  components,
  fallback,
}: {
  text: string;
  components: Components;
  fallback?: ReactNode;
}) {
  const rendered = useRef<HTMLSpanElement>(null);
  const [isEmpty, setIsEmpty] = useState(false);

  useLayoutEffect(() => {
    setIsEmpty((rendered.current?.textContent ?? "").trim() === "");
  }, [text]);

  return (
    <>
      <span ref={rendered} className={isEmpty ? "hidden" : undefined}>
        <ReactMarkdown components={components}>{text}</ReactMarkdown>
      </span>
      {isEmpty ? (fallback ?? text) : null}
    </>
  );
}

/** Agent prose on one line — a timeline row. All markdown collapses to inline content. */
export const InlineMarkdown = memo(function InlineMarkdown({
  text,
  fallback,
}: {
  text: string;
  fallback?: ReactNode;
}) {
  return (
    <Rendered text={text} components={inlineComponents} fallback={fallback} />
  );
});

/** Agent prose in a card or detail pane, where paragraphs should stay apart. */
export const BlockMarkdown = memo(function BlockMarkdown({
  text,
}: {
  text: string;
}) {
  return <Rendered text={text} components={blockComponents} />;
});
