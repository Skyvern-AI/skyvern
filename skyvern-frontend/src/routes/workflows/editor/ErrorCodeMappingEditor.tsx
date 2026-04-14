import { lintGutter } from "@codemirror/lint";
import type { Extension } from "@uiw/react-codemirror";

import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

import { errorCodeMappingLinter } from "./errorCodeMappingLinter";
import { ErrorCodeMappingValidation } from "./ErrorCodeMappingValidation";

// Module-level constant so React does not see a fresh array (and a fresh
// `lintGutter()` instance) on every render. Passing a new extension tuple
// into CodeMirror each cycle would trigger unnecessary editor-state churn.
const EXTRA_EXTENSIONS: Extension[] = [errorCodeMappingLinter, lintGutter()];

/**
 * Thin wrapper around `CodeEditor` that adds two things for authoring
 * `error_code_mapping` JSON:
 *
 *   1. An inline CodeMirror linter that draws a squiggly underline on the
 *      exact character range of any key with surrounding whitespace, plus a
 *      gutter marker and a hover tooltip explaining the problem.
 *   2. A persistent summary box below the editor listing every problem the
 *      save-time validator reports (parse errors, wrong shape, whitespace
 *      keys) — same text the save-time toast shows.
 *
 * Used by every block type that edits error_code_mapping: task, validation,
 * action, navigation, login, file_download.
 */
export function ErrorCodeMappingEditor({
  label,
  value,
  onChange,
  readOnly = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
}) {
  return (
    <div>
      <CodeEditor
        language="json"
        value={value}
        onChange={readOnly ? undefined : onChange}
        className="nopan"
        readOnly={readOnly}
        extraExtensions={EXTRA_EXTENSIONS}
      />
      <ErrorCodeMappingValidation label={label} value={value} />
    </div>
  );
}
