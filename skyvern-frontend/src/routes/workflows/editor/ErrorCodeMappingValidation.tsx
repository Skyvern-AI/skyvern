import { ExclamationTriangleIcon } from "@radix-ui/react-icons";
import { useMemo } from "react";

import { validateErrorCodeMapping } from "./validateErrorCodeMapping";

/**
 * Inline validation display for the error_code_mapping CodeEditor. Renders
 * the same messages that `getWorkflowErrors` produces at save time, directly
 * below the editor, so the user sees problems next to the field instead of
 * only in the destructive toast after clicking Save.
 *
 * Label-prefix ("block_X: ") is stripped so the message reads naturally
 * under the field. The component renders nothing when the value is clean.
 */
export function ErrorCodeMappingValidation({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  // Memoize so large mappings don't re-validate on every parent render —
  // only when the value (or label used for error-prefix stripping)
  // actually changes. `"null"` is the disabled sentinel and skips the
  // validator entirely.
  const errors = useMemo(
    () => (value === "null" ? [] : validateErrorCodeMapping(label, value)),
    [label, value],
  );
  if (errors.length === 0) {
    return null;
  }
  const prefix = `${label}: `;
  const strip = (err: string) =>
    err.startsWith(prefix) ? err.slice(prefix.length) : err;
  return (
    <div className="mb-2 mt-1 flex items-start gap-1 rounded-md border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-400">
      <ExclamationTriangleIcon className="mt-0.5 h-3 w-3 shrink-0" />
      {errors.length === 1 ? (
        <div className="flex-1">{strip(errors[0]!)}</div>
      ) : (
        <div className="flex-1">
          <div className="font-medium">
            {errors.length} problems with Error Messages
          </div>
          <ul className="mt-1 list-disc pl-4">
            {errors.map((err) => (
              <li key={err}>{strip(err)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
