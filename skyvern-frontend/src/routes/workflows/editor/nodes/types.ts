export type NodeBaseData = {
  label: string;
  continueOnFailure: boolean;
  editable: boolean;
};

export const errorMappingExampleValue = {
  sample_invalid_credentials: "if the credentials are incorrect, terminate",
} as const;

export const dataSchemaExampleValue = {
  type: "object",
  properties: {
    sample: { type: "string" },
  },
} as const;
