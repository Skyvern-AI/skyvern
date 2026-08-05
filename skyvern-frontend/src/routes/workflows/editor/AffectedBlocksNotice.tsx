import { type AffectedBlock } from "./jinjaReferences";

function AffectedBlocksNotice({
  affectedBlocks,
}: {
  affectedBlocks: AffectedBlock[];
}) {
  if (affectedBlocks.length === 0) {
    return null;
  }
  return (
    <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3">
      <p className="mb-2 font-medium text-amber-500">
        The following blocks reference this item and will be updated:
      </p>
      <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
        {affectedBlocks.map((block) => (
          <li key={block.nodeId}>
            <span className="font-medium">{block.label}</span>
            <span className="text-slate-400">
              {" "}
              (
              {[
                block.hasParameterKeyReference && "input selector",
                block.hasJinjaReference && "text field",
              ]
                .filter(Boolean)
                .join(", ")}
              )
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export { AffectedBlocksNotice };
