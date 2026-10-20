import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownTreeNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: MarkdownTreeNode[];
}

export interface CopilotMarkdownReveal {
  shown: number;
  gradientStart: number;
  onCharacterCount: (count: number) => void;
}

function markdownRevealPlugin(reveal: CopilotMarkdownReveal) {
  return () => (tree: MarkdownTreeNode) => {
    let offset = 0;
    let lastGradientNode: MarkdownTreeNode | null = null;

    const revealChildren = (node: MarkdownTreeNode) => {
      if (!node.children) return;
      const visibleChildren: MarkdownTreeNode[] = [];

      for (const child of node.children) {
        if (child.type !== "text") {
          const offsetBeforeChild = offset;
          revealChildren(child);
          // An element with no text (an empty table cell, a divider) appears once the
          // reveal reaches it, so later cells in its row do not shift left.
          if (
            (child.children && child.children.length > 0) ||
            offsetBeforeChild < reveal.shown
          ) {
            visibleChildren.push(child);
          }
          continue;
        }

        const value = child.value ?? "";
        const start = offset;
        offset += value.length;
        const visibleLength = Math.min(
          value.length,
          Math.max(0, reveal.shown - start),
        );
        if (visibleLength === 0) continue;

        const stableLength = Math.min(
          visibleLength,
          Math.max(0, reveal.gradientStart - start),
        );
        if (stableLength > 0) {
          visibleChildren.push({
            type: "text",
            value: value.slice(0, stableLength),
          });
        }

        const gradientLength = visibleLength - stableLength;
        // Whitespace fades invisibly, and a span between table cells is an extra cell.
        if (gradientLength > 0 && value.trim() === "") {
          visibleChildren.push({
            type: "text",
            value: value.slice(stableLength, visibleLength),
          });
          continue;
        }
        for (let i = 0; i < gradientLength; i += 1) {
          const characterOffset = start + stableLength + i;
          const progress =
            (characterOffset - reveal.gradientStart + 1) /
            Math.max(1, reveal.shown - reveal.gradientStart);
          const opacity = Math.max(0.12, 1 - progress * 0.88);
          const gradientNode: MarkdownTreeNode = {
            type: "element",
            tagName: "span",
            properties: { style: `opacity: ${opacity.toFixed(2)}` },
            children: [
              {
                type: "text",
                value: value[stableLength + i],
              },
            ],
          };
          visibleChildren.push(gradientNode);
          lastGradientNode = gradientNode;
        }
      }

      node.children = visibleChildren;
    };

    revealChildren(tree);
    const gradientEdge = lastGradientNode as MarkdownTreeNode | null;
    if (gradientEdge) {
      gradientEdge.properties = {
        ...gradientEdge.properties,
        "data-testid": "copilot-terminal-prose-gradient",
      };
    }
    reveal.onCharacterCount(offset);
  };
}

// Agent replies are Markdown, and Tailwind's preflight strips the browser defaults, so every
// construct the agent can write needs its look spelled out here or it renders as bare text.
const MARKDOWN_CLASSES = [
  "whitespace-normal [&>:first-child]:mt-0 [&>:last-child]:mb-0",
  "[&_a]:underline [&_a]:underline-offset-2",
  "[&_code]:rounded [&_code]:bg-slate-500/15 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.92em]",
  "[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:whitespace-pre-wrap [&_pre]:rounded-md [&_pre]:bg-slate-500/15 [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_p+p]:mt-3",
  "[&_li+li]:mt-1 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-5",
  "[&_li>ol]:my-1 [&_li>ul]:my-1 [&_li.task-list-item]:list-none [&_li.task-list-item>input]:mr-2",
  "[&_:is(h1,h2,h3,h4,h5,h6)]:mb-2 [&_:is(h1,h2,h3,h4,h5,h6)]:mt-4 [&_:is(h1,h2,h3,h4,h5,h6)]:font-semibold [&_h1]:text-[1.3em] [&_h2]:text-[1.15em] [&_h3]:text-[1.05em]",
  "[&_blockquote]:my-3 [&_blockquote]:border-l-2 [&_blockquote]:border-slate-500/40 [&_blockquote]:pl-3 [&_blockquote]:opacity-80",
  "[&_hr]:my-4 [&_hr]:border-slate-500/25",
  "[&_table]:text-left",
  "[&_th]:whitespace-nowrap [&_th]:border-b [&_th]:border-slate-500/25 [&_th]:bg-slate-500/10 [&_th]:px-3 [&_th]:py-2 [&_th]:font-semibold",
  "[&_td]:px-3 [&_td]:py-2 [&_td]:align-top [&_tr+tr>td]:border-t [&_tr+tr>td]:border-slate-500/15",
].join(" ");

const TABLE_REM_PER_COLUMN = 8;

function columnCount(table: MarkdownTreeNode | undefined): number {
  const rows = (table?.children ?? []).flatMap((section) =>
    (section.children ?? []).filter((row) => row.tagName === "tr"),
  );
  return (rows[0]?.children ?? []).filter(
    (cell) => cell.tagName === "th" || cell.tagName === "td",
  ).length;
}

// Styling the scrollbar keeps it visible in Chrome and Safari on systems that hide
// scrollbars until use; Firefox keeps its own.
const TABLE_FRAME_CLASSES =
  "my-3 w-fit max-w-full overflow-x-auto rounded-md border border-slate-500/25 [.sr-only_&]:overflow-visible [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-slate-500/40 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar]:h-2";

// Fades the right edge of the table, leaving the scrollbar strip below it solid.
const TABLE_MORE_TO_SCROLL_CLASSES =
  "[mask-image:linear-gradient(to_right,#000_calc(100%-3rem),transparent),linear-gradient(#000,#000)] [mask-position:top,bottom] [mask-repeat:no-repeat] [mask-size:100%_calc(100%-0.625rem),100%_0.625rem]";

// A table takes its natural width when the panel has room and wraps its cells when it
// does not, but never below a readable width per column: past that it scrolls sideways
// inside its own frame, with a scrollbar and a faded edge to say there is more. The
// screen-reader copy of a reply is 1px wide, where a scroll frame would only add a hidden
// tab stop, so it does not scroll there.
function TableFrame({
  columns,
  children,
}: {
  columns: number;
  children: ReactNode;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const [moreToScroll, setMoreToScroll] = useState(false);

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    const update = () =>
      setMoreToScroll(
        element.scrollLeft + element.clientWidth < element.scrollWidth - 1,
      );
    update();
    element.addEventListener("scroll", update);
    // The table grows as a reply streams in and the panel can be resized. The table is
    // watched as well as its sizer because an unbreakable cell overflows the sizer.
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    for (const target of [
      element,
      element.firstElementChild,
      element.querySelector("table"),
    ]) {
      if (target) observer?.observe(target);
    }
    return () => {
      element.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, []);

  return (
    <div
      ref={frame}
      className={
        moreToScroll
          ? `${TABLE_FRAME_CLASSES} ${TABLE_MORE_TO_SCROLL_CLASSES}`
          : TABLE_FRAME_CLASSES
      }
    >
      <div
        className="w-max"
        style={{
          maxWidth: `max(100%, ${columns * TABLE_REM_PER_COLUMN}rem)`,
        }}
      >
        {children}
      </div>
    </div>
  );
}

const MARKDOWN_COMPONENTS: Components = {
  img: () => null,
  table: ({ node, ...props }) => (
    <TableFrame columns={columnCount(node as MarkdownTreeNode)}>
      <table {...props} />
    </TableFrame>
  ),
};

export function CopilotMarkdown({
  text,
  reveal,
}: {
  text: string;
  reveal?: CopilotMarkdownReveal;
}) {
  return (
    <div className={MARKDOWN_CLASSES}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={reveal ? [markdownRevealPlugin(reveal)] : undefined}
        components={MARKDOWN_COMPONENTS}
        skipHtml
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
