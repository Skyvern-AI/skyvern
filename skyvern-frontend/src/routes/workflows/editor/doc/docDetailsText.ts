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
  // The fix line names the Credential chip only where the step shows one.
  noCredential: (editable: boolean, chip: boolean) =>
    !editable
      ? "This step has no saved credential."
      : chip
        ? "This step has no saved credential. Click Credential under the step's title to choose one."
        : "This step has no saved credential. Open this step's settings and choose a credential.",
} as const;
