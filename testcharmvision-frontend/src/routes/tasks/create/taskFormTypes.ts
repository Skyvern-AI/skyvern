import { ProxyLocation } from "@/api/types";
import { z } from "zod";

const createNewTaskFormSchemaBase = z.object({
  url: z.string().url({
    message: "Invalid URL",
  }),
  webhookCallbackUrl: z.string().or(z.null()),
  navigationGoal: z.string().or(z.null()),
  dataExtractionGoal: z.string().or(z.null()),
  navigationPayload: z.string().or(z.null()),
  extractedInformationSchema: z.string().or(z.null()),
  extraHttpHeaders: z.string().or(z.null()),
  maxStepsOverride: z.number().or(z.null()).optional(),
  totpIdentifier: z.string().or(z.null()),
  cdpAddress: z.string().or(z.null()),
  errorCodeMapping: z.string().or(z.null()),
  proxyLocation: z
    .union([
      z.nativeEnum(ProxyLocation),
      z.object({
        country: z.string(),
        subdivision: z.string().optional(),
        city: z.string().optional(),
      }),
    ])
    .nullable(),
  includeActionHistoryInVerification: z.boolean().or(z.null()).default(false),
  maxScreenshotScrolls: z.number().or(z.null()).default(null),
});

const savedTaskFormSchemaBase = createNewTaskFormSchemaBase.extend({
  title: z.string().min(1, "Title is required"),
  description: z.string(),
});

function refineTaskFormValues(
  values: CreateNewTaskFormValues | SavedTaskFormValues,
  ctx: z.RefinementCtx,
) {
  const {
    navigationGoal,
    dataExtractionGoal,
    extractedInformationSchema,
    errorCodeMapping,
  } = values;
  if (!navigationGoal && !dataExtractionGoal) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message:
        "At least one of navigation goal or data extraction goal must be provided",
      path: ["navigationGoal"],
    });
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message:
        "At least one of navigation goal or data extraction goal must be provided",
      path: ["dataExtractionGoal"],
    });
  }
  if (extractedInformationSchema) {
    try {
      JSON.parse(extractedInformationSchema);
    } catch (e) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Invalid JSON",
        path: ["extractedInformationSchema"],
      });
    }
  }
  if (errorCodeMapping) {
    try {
      JSON.parse(errorCodeMapping);
    } catch (e) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Invalid JSON",
        path: ["errorCodeMapping"],
      });
    }
  }
}

export const createNewTaskFormSchema =
  createNewTaskFormSchemaBase.superRefine(refineTaskFormValues);

export const savedTaskFormSchema =
  savedTaskFormSchemaBase.superRefine(refineTaskFormValues);

export type CreateNewTaskFormValues = z.infer<
  typeof createNewTaskFormSchemaBase
>;
export type SavedTaskFormValues = z.infer<typeof savedTaskFormSchemaBase>;
