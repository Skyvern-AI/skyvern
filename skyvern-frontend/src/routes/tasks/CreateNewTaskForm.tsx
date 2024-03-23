import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  dataExtractionGoalDescription,
  extractedInformationSchemaDescription,
  navigationGoalDescription,
  navigationPayloadDescription,
  urlDescription,
  webhookCallbackUrlDescription,
} from "./descriptionHelperContent";
import { Textarea } from "@/components/ui/textarea";
import { useMutation } from "@tanstack/react-query";
import { client } from "@/api/AxiosClient";
import { useToast } from "@/components/ui/use-toast";

const createNewTaskFormSchema = z.object({
  url: z.string().url({
    message: "Invalid URL",
  }),
  webhookCallbackUrl: z.string().optional(), // url maybe, but shouldn't be validated as one
  navigationGoal: z.string().optional(),
  dataExtractionGoal: z.string().optional(),
  navigationPayload: z.string().optional(),
  extractedInformationSchema: z.string().optional(),
});

export type CreateNewTaskFormValues = z.infer<typeof createNewTaskFormSchema>;

type Props = {
  initialValues: CreateNewTaskFormValues;
};

function createTaskRequestObject(formValues: CreateNewTaskFormValues) {
  return {
    url: formValues.url,
    webhook_callback_url: formValues.webhookCallbackUrl ?? "",
    navigation_goal: formValues.navigationGoal ?? "",
    data_extraction_goal: formValues.dataExtractionGoal ?? "",
    proxy_location: "NONE",
    navigation_payload: formValues.navigationPayload ?? "",
    extracted_information_schema: formValues.extractedInformationSchema ?? "",
  };
}

function CreateNewTaskForm({ initialValues }: Props) {
  const { toast } = useToast();

  const form = useForm<CreateNewTaskFormValues>({
    resolver: zodResolver(createNewTaskFormSchema),
    defaultValues: initialValues,
  });

  const mutation = useMutation({
    mutationFn: (formValues: CreateNewTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
    onSuccess: (response) => {
      toast({
        title: "Task Created",
        description: `${response.data.task_id} created successfully.`,
      });
    },
  });

  function onSubmit(values: CreateNewTaskFormValues) {
    mutation.mutate(values);
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-8">
        <FormField
          control={form.control}
          name="url"
          render={({ field }) => (
            <FormItem>
              <FormLabel>URL*</FormLabel>
              <FormControl>
                <Input placeholder="example.com" {...field} />
              </FormControl>
              <FormDescription>{urlDescription}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="webhookCallbackUrl"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Webhook Callback URL</FormLabel>
              <FormControl>
                <Input placeholder="example.com" {...field} />
              </FormControl>
              <FormDescription>{webhookCallbackUrlDescription}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="navigationGoal"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Navigation Goal</FormLabel>
              <FormControl>
                <Textarea rows={5} placeholder="Navigation Goal" {...field} />
              </FormControl>
              <FormDescription>{navigationGoalDescription}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="dataExtractionGoal"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Data Extraction Goal</FormLabel>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Data Extraction Goal"
                  {...field}
                />
              </FormControl>
              <FormDescription>{dataExtractionGoalDescription}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="navigationPayload"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Navigation Payload</FormLabel>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Navigation Payload"
                  {...field}
                />
              </FormControl>
              <FormDescription>{navigationPayloadDescription}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="extractedInformationSchema"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Extracted Information Schema</FormLabel>
              <FormControl>
                <Textarea
                  placeholder="Extracted Information Schema"
                  rows={5}
                  {...field}
                />
              </FormControl>
              <FormDescription>
                {extractedInformationSchemaDescription}
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <div className="flex justify-end gap-3">
          <Button variant="outline">Copy cURL</Button>
          <Button type="submit">Create New Task</Button>
        </div>
      </form>
    </Form>
  );
}

export { CreateNewTaskForm };
