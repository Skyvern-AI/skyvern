// Every word the Details disclosure shows, so a naming decision is one edit.
const toggle = "Details";
const morePanel = "More details in the side panel";

export const docDetailsText = {
  toggle,
  toggleFor: (name: string) => `Details for ${name}`,
  tooltip: "Key settings, inputs, outputs and custom settings",
  inputs: "Inputs",
  outputs: "Outputs",
  outputFields: "Fields",
  custom: "Custom settings",
  allDefault: "Every other setting is at its default.",
  notSet: "Not set",
  morePanel,
  // A read-only Doc's Details has no side-panel link, so its fix line does not point there.
  noCredential: (editable: boolean) =>
    editable
      ? `This step has no saved credential. Open ${toggle}, then ${morePanel} to choose one.`
      : "This step has no saved credential.",
} as const;
