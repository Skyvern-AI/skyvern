import { pythonLanguage } from "@codemirror/lang-python";
import { lintGutter, linter, type Diagnostic } from "@codemirror/lint";
import type { Extension } from "@uiw/react-codemirror";

type SourceRange = { from: number; to: number };
type JinjaTag = SourceRange & { body: string; command: string };
type JinjaBranchGroup = {
  branches: SourceRange[];
  hasElse: boolean;
  repeats: boolean;
};
type MappedVariant = {
  text: string;
  offsets: number[];
  choices: number[];
};

const MAX_JINJA_VARIANTS = 32;
const JINJA_TOKEN_PATTERN = /{{[\s\S]*?}}|{%[\s\S]*?%}|{#[\s\S]*?#}/g;
const JINJA_STATEMENT_PATTERN = /{%[\s\S]*?%}/g;

function pythonCommentRanges(source: string): SourceRange[] {
  const ranges: SourceRange[] = [];
  pythonLanguage.parser.parse(source).iterate({
    enter(node) {
      if (node.name === "Comment")
        ranges.push({ from: node.from, to: node.to });
    },
  });
  return ranges;
}

function isInsideRange(index: number, ranges: SourceRange[]): boolean {
  return ranges.some((range) => index >= range.from && index < range.to);
}

function jinjaStatementBody(tag: string): string {
  return tag.slice(2, -2).trim().replace(/^-|-$/g, "").trim();
}

function jinjaRawRanges(
  source: string,
  commentRanges: SourceRange[],
): SourceRange[] {
  const ranges: SourceRange[] = [];
  let rawBodyStart: number | null = null;

  for (const match of source.matchAll(JINJA_STATEMENT_PATTERN)) {
    if (isInsideRange(match.index, commentRanges)) continue;
    const command = jinjaStatementBody(match[0]).split(/\s+/, 1)[0];
    if (rawBodyStart == null && command === "raw") {
      rawBodyStart = match.index + match[0].length;
    } else if (rawBodyStart != null && command === "endraw") {
      ranges.push({ from: rawBodyStart, to: match.index });
      rawBodyStart = null;
    }
  }

  return ranges;
}

function jinjaTags(source: string, ignoredRanges: SourceRange[]): JinjaTag[] {
  return [...source.matchAll(JINJA_STATEMENT_PATTERN)]
    .filter((match) => !isInsideRange(match.index, ignoredRanges))
    .map((match) => {
      const body = jinjaStatementBody(match[0]);
      return {
        from: match.index,
        to: match.index + match[0].length,
        body,
        command: body.split(/\s+/, 1)[0] ?? "",
      };
    });
}

function jinjaWhitespaceControlRanges(
  source: string,
  ignoredRanges: SourceRange[],
): SourceRange[] {
  const ranges: SourceRange[] = [];

  for (const match of source.matchAll(JINJA_TOKEN_PATTERN)) {
    if (isInsideRange(match.index, ignoredRanges)) continue;
    const tagEnd = match.index + match[0].length;
    if (match[0][2] === "-") {
      let from = match.index;
      while (from > 0 && /\s/.test(source[from - 1]!)) from -= 1;
      ranges.push({ from, to: match.index });
    }
    if (match[0][match[0].length - 3] === "-") {
      let to = tagEnd;
      while (to < source.length && /\s/.test(source[to]!)) to += 1;
      ranges.push({ from: tagEnd, to });
    }
  }

  return ranges;
}

function templateStructure(tags: JinjaTag[]): {
  branchGroups: JinjaBranchGroup[];
  nonEmittingRanges: SourceRange[];
} {
  const branchGroups: JinjaBranchGroup[] = [];
  const branchStack: Array<{
    endCommand: "endif" | "endfor";
    branchStart: number;
    branches: SourceRange[];
    hasElse: boolean;
  }> = [];
  const nonEmittingRanges: SourceRange[] = [];
  const nonEmittingStack: Array<{
    endCommand: "endmacro" | "endset";
    bodyStart: number;
  }> = [];

  for (const tag of tags) {
    const branch = branchStack[branchStack.length - 1];
    if (tag.command === "if" || tag.command === "for") {
      branchStack.push({
        endCommand: tag.command === "if" ? "endif" : "endfor",
        branchStart: tag.to,
        branches: [],
        hasElse: false,
      });
    } else if (
      branch &&
      (tag.command === "else" ||
        (tag.command === "elif" && branch.endCommand === "endif"))
    ) {
      branch.branches.push({ from: branch.branchStart, to: tag.from });
      branch.branchStart = tag.to;
      branch.hasElse ||= tag.command === "else";
    } else if (branch && tag.command === branch.endCommand) {
      branch.branches.push({ from: branch.branchStart, to: tag.from });
      branchStack.pop();
      branchGroups.push({
        branches: branch.branches,
        hasElse: branch.hasElse,
        repeats: branch.endCommand === "endfor",
      });
    }

    const nonEmitting = nonEmittingStack[nonEmittingStack.length - 1];
    const startsBlockSet = tag.command === "set" && !tag.body.includes("=");
    if (tag.command === "macro" || startsBlockSet) {
      nonEmittingStack.push({
        endCommand: tag.command === "macro" ? "endmacro" : "endset",
        bodyStart: tag.to,
      });
    } else if (nonEmitting && tag.command === nonEmitting.endCommand) {
      nonEmittingRanges.push({ from: nonEmitting.bodyStart, to: tag.from });
      nonEmittingStack.pop();
    }
  }

  return { branchGroups, nonEmittingRanges };
}

function initialVariant(source: string): MappedVariant {
  return {
    text: source,
    offsets: Array.from({ length: source.length }, (_, index) => index),
    choices: [],
  };
}

function removeMappedRange(
  variant: MappedVariant,
  range: SourceRange,
): MappedVariant {
  const characters: string[] = [];
  const offsets: number[] = [];
  for (let index = 0; index < variant.text.length; index++) {
    const offset = variant.offsets[index]!;
    if (offset >= range.from && offset < range.to) continue;
    characters.push(variant.text[index]!);
    offsets.push(offset);
  }
  return { ...variant, text: characters.join(""), offsets };
}

function replaceMappedRange(
  variant: MappedVariant,
  range: SourceRange,
): MappedVariant {
  const characters: string[] = [];
  const offsets: number[] = [];
  let inserted = false;
  for (let index = 0; index < variant.text.length; index++) {
    const offset = variant.offsets[index]!;
    if (offset >= range.from && offset < range.to) {
      if (!inserted) {
        characters.push("_");
        offsets.push(range.from);
        inserted = true;
      }
      continue;
    }
    characters.push(variant.text[index]!);
    offsets.push(offset);
  }
  return { ...variant, text: characters.join(""), offsets };
}

function repeatMappedRange(
  variant: MappedVariant,
  range: SourceRange,
): MappedVariant {
  const characters: string[] = [];
  const offsets: number[] = [];
  let insertAt = -1;
  for (let index = 0; index < variant.text.length; index++) {
    const offset = variant.offsets[index]!;
    if (offset >= range.from && offset < range.to) {
      characters.push(variant.text[index]!);
      offsets.push(offset);
      insertAt = index + 1;
    }
  }
  if (insertAt < 0) return variant;
  return {
    ...variant,
    text: `${variant.text.slice(0, insertAt)}${characters.join("")}${variant.text.slice(insertAt)}`,
    offsets: [
      ...variant.offsets.slice(0, insertAt),
      ...offsets,
      ...variant.offsets.slice(insertAt),
    ],
  };
}

function boundedVariants(candidates: MappedVariant[]): MappedVariant[] {
  if (candidates.length <= MAX_JINJA_VARIANTS) return candidates;

  const remaining = [...candidates];
  const uncoveredChoices = new Set(
    candidates.flatMap((candidate) =>
      candidate.choices.map((choice, index) => `${index}:${choice}`),
    ),
  );
  const selected: MappedVariant[] = [];

  while (
    selected.length < MAX_JINJA_VARIANTS &&
    remaining.length > 0 &&
    uncoveredChoices.size > 0
  ) {
    let bestIndex = 0;
    let bestScore = -1;
    for (let index = 0; index < remaining.length; index++) {
      const score = remaining[index]!.choices.filter((choice, choiceIndex) =>
        uncoveredChoices.has(`${choiceIndex}:${choice}`),
      ).length;
      if (score > bestScore) {
        bestIndex = index;
        bestScore = score;
      }
    }

    const [best] = remaining.splice(bestIndex, 1);
    selected.push(best!);
    best!.choices.forEach((choice, index) =>
      uncoveredChoices.delete(`${index}:${choice}`),
    );
  }

  selected.push(...remaining.slice(0, MAX_JINJA_VARIANTS - selected.length));
  return selected;
}

function basePythonVariant(
  source: string,
  ignoredRanges: SourceRange[],
  removedRanges: SourceRange[],
): MappedVariant {
  let variant = initialVariant(source);
  for (const range of removedRanges)
    variant = removeMappedRange(variant, range);

  for (const match of source.matchAll(JINJA_TOKEN_PATTERN)) {
    if (isInsideRange(match.index, ignoredRanges)) continue;
    const range = { from: match.index, to: match.index + match[0].length };
    variant = match[0].startsWith("{{")
      ? replaceMappedRange(variant, range)
      : removeMappedRange(variant, range);
  }

  return variant;
}

function pythonSourceVariants(
  source: string,
  commentRanges: SourceRange[],
): MappedVariant[] {
  const rawRanges = jinjaRawRanges(source, commentRanges);
  const ignoredRanges = [...commentRanges, ...rawRanges];
  const tags = jinjaTags(source, ignoredRanges);
  const { branchGroups, nonEmittingRanges } = templateStructure(tags);
  const removedRanges = [
    ...jinjaWhitespaceControlRanges(source, ignoredRanges),
    ...nonEmittingRanges,
  ];
  let variants = [basePythonVariant(source, ignoredRanges, removedRanges)];

  for (const group of branchGroups) {
    const candidates: MappedVariant[] = [];
    const choiceCount = group.branches.length + (group.hasElse ? 0 : 1);

    for (const variant of variants) {
      for (let selected = 0; selected < choiceCount; selected++) {
        let next = variant;
        for (let index = 0; index < group.branches.length; index++) {
          if (index !== selected) {
            next = removeMappedRange(next, group.branches[index]!);
          }
        }
        candidates.push({
          ...next,
          choices: [...variant.choices, selected],
        });
      }

      if (group.repeats) {
        let repeated = variant;
        for (let index = 1; index < group.branches.length; index++) {
          repeated = removeMappedRange(repeated, group.branches[index]!);
        }
        repeated = repeatMappedRange(repeated, group.branches[0]!);
        candidates.push({
          ...repeated,
          choices: [...variant.choices, choiceCount],
        });
      }
    }

    variants = boundedVariants(candidates);
  }

  return variants;
}

function tokenRange(
  source: string,
  index: number,
): { from: number; to: number } {
  const isWordCharacter = (character: string) => /[\w]/.test(character);
  let from = index;
  let to = index + 1;

  if (isWordCharacter(source[index]!)) {
    while (from > 0 && isWordCharacter(source[from - 1]!)) from -= 1;
    while (to < source.length && isWordCharacter(source[to]!)) to += 1;
  }

  return { from, to };
}

function visibleRange(
  source: string,
  from: number,
  to: number,
): { from: number; to: number } {
  if (/\S/.test(source.slice(from, to))) return { from, to };

  if (from === to && from < source.length) {
    const next = source.slice(from).search(/\S/);
    if (next >= 0) return tokenRange(source, from + next);
  }

  for (let index = Math.min(from - 1, source.length - 1); index >= 0; index--) {
    if (/\S/.test(source[index]!)) return tokenRange(source, index);
  }

  return { from, to };
}

function originalRange(
  variant: MappedVariant,
  range: SourceRange,
  sourceLength: number,
): SourceRange {
  if (range.from === range.to) {
    const offset =
      range.from < variant.offsets.length
        ? variant.offsets[range.from]!
        : Math.min(
            sourceLength,
            (variant.offsets[variant.offsets.length - 1] ?? -1) + 1,
          );
    return { from: offset, to: offset };
  }

  const offsets = variant.offsets.slice(range.from, range.to);
  return {
    from: Math.min(...offsets),
    to: Math.min(sourceLength, Math.max(...offsets) + 1),
  };
}

function hasPythonCode(
  variant: MappedVariant,
  commentRanges: SourceRange[],
): boolean {
  for (let index = 0; index < variant.text.length; index++) {
    if (
      /\S/.test(variant.text[index]!) &&
      !isInsideRange(variant.offsets[index]!, commentRanges)
    ) {
      return true;
    }
  }
  return false;
}

export function getPythonSyntaxDiagnostics(source: string): Diagnostic[] {
  const diagnostics: Diagnostic[] = [];
  const seenRanges = new Set<string>();
  const commentRanges = pythonCommentRanges(source);

  for (const variant of pythonSourceVariants(source, commentRanges)) {
    if (!hasPythonCode(variant, commentRanges)) continue;
    const tree = pythonLanguage.parser.parse(variant.text);

    tree.iterate({
      enter(node) {
        if (!node.type.isError) return;
        const visible = visibleRange(variant.text, node.from, node.to);
        const range = originalRange(variant, visible, source.length);
        const key = `${range.from}:${range.to}`;
        if (seenRanges.has(key)) return;
        seenRanges.add(key);
        diagnostics.push({
          ...range,
          severity: "error",
          message: "Invalid Python syntax.",
          source: "Python",
        });
      },
    });
  }

  return diagnostics.sort((left, right) => left.from - right.from);
}

export const pythonSyntaxLinter = linter((view) =>
  getPythonSyntaxDiagnostics(view.state.doc.toString()),
);

export const pythonSyntaxExtensions: Extension[] = [
  pythonSyntaxLinter,
  lintGutter(),
];
