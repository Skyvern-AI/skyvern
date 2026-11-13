/** The block label a typed step name saves as: spaces and punctuation become `_`, accents drop, case is kept; a leading digit gets `step_`. */
export function docNameToLabel(name: string): string {
  return name
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/_{2,}/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^(?=\d)/, "step_");
}

/** The label to show under the field while typing, when it reads differently from the name beyond spaces becoming `_`. */
export function docNameLabelPreview(name: string): string | null {
  const label = docNameToLabel(name);
  return label && label !== name.trim().replace(/\s+/g, "_") ? label : null;
}

/** Why a label can't be a typed step's name: the title would read it differently, or the saved YAML would. */
export function docNameLabelProblem(label: string): string | null {
  if (!label) return "Use letters or numbers";
  // humanizeBlockLabel drops a version suffix, so the title would not show it.
  if (/_v\d+$/i.test(label))
    return "Add a word after the v and number: the title drops it";
  // The YAML writer leaves these unquoted, and the server's YAML 1.1 loader reads them as booleans.
  if (/^(yes|no|on|off)$/i.test(label))
    return "Add a word: Yes, No, On and Off save as true or false";
  return null;
}
