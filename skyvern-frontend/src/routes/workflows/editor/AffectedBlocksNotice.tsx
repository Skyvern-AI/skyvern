import { type AffectedBlock } from "./jinjaReferences";

const CODE_CLASS = "rounded bg-muted px-1 py-0.5 font-mono text-xs";
const LIST_CLASS = "list-outside list-disc space-y-1 pl-4";

function Names({ keys }: { keys: Array<string> }) {
  return (
    <>
      {keys.map((key, index) => (
        <span key={key}>
          {index > 0 && ", "}
          <code className={CODE_CLASS}>{key}</code>
        </span>
      ))}
    </>
  );
}

function Label({ block }: { block: AffectedBlock }) {
  return <span className="font-medium">{block.label}</span>;
}

function unreadKeys(block: AffectedBlock): Array<string> {
  return block.keys.filter(
    (key) =>
      !block.codeReadKeys.includes(key) && !block.credentialKeys.includes(key),
  );
}

function CodeBlockLines({ blocks }: { blocks: Array<AffectedBlock> }) {
  return (
    <>
      {blocks
        .filter((block) => block.codeReadKeys.length > 0)
        .map((block) => (
          <li key={`${block.nodeId}-reads`}>
            <Label block={block} /> reads <Names keys={block.codeReadKeys} /> in
            its code. Its code is not changed, so it will fail when it runs
            until you edit it.
          </li>
        ))}
      {blocks
        .filter((block) => block.credentialKeys.length > 0)
        .map((block) => {
          const many = block.credentialKeys.length > 1;
          return (
            <li key={`${block.nodeId}-credential`}>
              <Label block={block} /> lists{" "}
              <Names keys={block.credentialKeys} /> as{" "}
              {many ? "credential inputs" : "a credential input"}.{" "}
              {many ? "They are" : "It is"} removed from the inputs and the code
              is not changed, so it will fail if the code uses{" "}
              {many ? "them or their" : "it or its"} credential fields.
            </li>
          );
        })}
      {blocks
        .filter((block) => unreadKeys(block).length > 0)
        .map((block) => {
          const keys = unreadKeys(block);
          const many = keys.length > 1;
          return (
            <li key={`${block.nodeId}-unread`}>
              <Label block={block} /> lists <Names keys={keys} /> as{" "}
              {many ? "code inputs" : "a code input"} but does not read{" "}
              {many ? "them" : "it"} in its code. {many ? "They are" : "It is"}{" "}
              removed from the inputs.
            </li>
          );
        })}
    </>
  );
}

function OtherChangeLines({ block }: { block: AffectedBlock }) {
  return (
    <>
      {block.loopValueCleared && (
        <li>
          <Label block={block} /> loops over <Names keys={block.keys} />. Its
          loop value is cleared; set a new one before you save.
        </li>
      )}
      {!block.isCode && block.hasParameterKeyReference && (
        <li>
          <Label block={block} />: <Names keys={block.keys} />{" "}
          {block.keys.length > 1 ? "are" : "is"} removed from its inputs.
        </li>
      )}
      {block.inputShadowedKeys.length > 0 && (
        <li>
          <Label block={block} /> will read the input{" "}
          <Names keys={block.inputShadowedKeys} /> instead of the deleted
          block's output.
        </li>
      )}
    </>
  );
}

function AffectedBlocksNotice({
  affectedBlocks,
}: {
  affectedBlocks: AffectedBlock[];
}) {
  if (affectedBlocks.length === 0) {
    return null;
  }
  const codeBlocks = affectedBlocks.filter(
    (block) =>
      block.isCode &&
      (block.codeReadKeys.length > 0 || block.hasParameterKeyReference),
  );
  const textBlocks = affectedBlocks.filter(
    (block) => block.changedTexts.length > 0,
  );
  const otherBlocks = affectedBlocks.filter(
    (block) =>
      block.loopValueCleared ||
      block.inputShadowedKeys.length > 0 ||
      (!block.isCode && block.hasParameterKeyReference),
  );
  return (
    <div
      role="region"
      aria-label="Blocks that use this item"
      tabIndex={0}
      className="max-h-[50vh] space-y-3 overflow-y-auto rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-slate-300"
    >
      <p className="font-medium text-amber-700 dark:text-amber-300">
        These blocks use this item:
      </p>
      {codeBlocks.length > 0 && (
        <section>
          <h3 className="mb-1 font-medium">Code blocks</h3>
          <ul className={LIST_CLASS}>
            <CodeBlockLines blocks={codeBlocks} />
          </ul>
        </section>
      )}
      {textBlocks.length > 0 && (
        <section>
          <h3 className="mb-1 font-medium">Text after the delete</h3>
          <ul className={LIST_CLASS}>
            {textBlocks.flatMap((block) =>
              block.changedTexts.map((text, index) => (
                <li key={`${block.nodeId}-${index}`}>
                  <Label block={block} />:{" "}
                  {text === "" ? (
                    <span className="text-slate-500 dark:text-slate-400">
                      (now empty)
                    </span>
                  ) : (
                    <code
                      className={`${CODE_CLASS} whitespace-pre-wrap break-words`}
                    >
                      {text}
                    </code>
                  )}
                </li>
              )),
            )}
          </ul>
        </section>
      )}
      {otherBlocks.length > 0 && (
        <section>
          <h3 className="mb-1 font-medium">Other changes</h3>
          <ul className={LIST_CLASS}>
            {otherBlocks.map((block) => (
              <OtherChangeLines key={block.nodeId} block={block} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export { AffectedBlocksNotice };
