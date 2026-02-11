import { z } from "zod";

export const taskTemplateFormSchema = z.object({
  title: z.string().min(1, "Title can't be empty"),
  description: z.string(),
});

export type TaskTemplateFormValues = z.infer<typeof taskTemplateFormSchema>;
