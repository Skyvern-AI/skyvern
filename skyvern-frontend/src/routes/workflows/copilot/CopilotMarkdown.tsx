import ReactMarkdown from "react-markdown";
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
          if (
            (child.children && child.children.length > 0) ||
            (!child.children && offsetBeforeChild < reveal.shown)
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

export function CopilotMarkdown({
  text,
  reveal,
}: {
  text: string;
  reveal?: CopilotMarkdownReveal;
}) {
  return (
    <div className="whitespace-normal [&_a]:underline [&_a]:underline-offset-2 [&_code]:rounded [&_code]:bg-slate-500/15 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.92em] [&_li+li]:mt-1 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_p+p]:mt-3 [&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:whitespace-pre-wrap [&_pre]:rounded-md [&_pre]:bg-slate-500/15 [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={reveal ? [markdownRevealPlugin(reveal)] : undefined}
        components={{ img: () => null }}
        skipHtml
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
