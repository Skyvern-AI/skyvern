import { TaskApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { stringify as convertToYAML } from "yaml";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { zodResolver } from "@hookform/resolvers/zod";
import { DotsHorizontalIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useId, useState } from "react";
import { useForm } from "react-hook-form";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import {
  TaskTemplateFormValues,
  taskTemplateFormSchema,
} from "../create/TaskTemplateFormSchema";
import { useNavigate } from "react-router-dom";

function createTaskTemplateRequestObject(
  values: TaskTemplateFormValues,
  task: TaskApiResponse,
) {
  return {
    title: values.title,
    description: values.description,
    is_saved_task: true,
    webhook_callback_url: task.request.webhook_callback_url,
    proxy_location: task.request.proxy_location,
    workflow_definition: {
      version: 2,
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "json",
          key: "navigation_payload",
          default_value: JSON.stringify(task.request.navigation_payload),
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: values.title,
          url: task.request.url,
          navigation_goal: task.request.navigation_goal,
          data_extraction_goal:
            task.request.data_extraction_goal === ""
              ? null
              : task.request.data_extraction_goal,
          data_schema:
            task.request.extracted_information_schema === ""
              ? null
              : task.request.extracted_information_schema,
        },
      ],
    },
  };
}

type Props = {
  task: TaskApiResponse;
};

function TaskActions({ task }: Props) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();
  const form = useForm<TaskTemplateFormValues>({
    resolver: zodResolver(taskTemplateFormSchema),
    defaultValues: {
      title: "",
      description: "",
    },
  });

  const mutation = useMutation({
    mutationFn: async (values: TaskTemplateFormValues) => {
      const request = createTaskTemplateRequestObject(values, task);
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(request);
      return client
        .post("/workflows", yaml, {
          headers: {
            "Content-Type": "text/plain",
          },
        })
        .then((response) => response.data);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "There was an error while saving changes",
        description: error.message,
      });
    },
    onSuccess: () => {
      toast({
        variant: "success",
        title: "Template saved",
        description: "Template saved successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      setOpen(false);
    },
  });

  function handleSubmit(values: TaskTemplateFormValues) {
    mutation.mutate(values);
  }

  return (
    <div className="flex">
      <Dialog open={open} onOpenChange={setOpen}>
        <DropdownMenu>
          <DropdownMenuTrigger asChild className="ml-auto">
            <Button size="icon" variant="ghost">
              <DotsHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-56">
            <DropdownMenuLabel>Task Actions</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DialogTrigger asChild>
              <DropdownMenuItem
                onSelect={() => {
                  setOpen(true);
                }}
              >
                Save as Template
              </DropdownMenuItem>
            </DialogTrigger>
            <DropdownMenuItem
              onSelect={() => {
                navigate(`/tasks/create/retry/${task.task_id}`);
              }}
            >
              Rerun Task
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save Task as Template</DialogTitle>
            <DialogDescription>
              Save this task definition as a template that can be used later.
            </DialogDescription>
          </DialogHeader>
          <Separator />
          <Form {...form}>
            <form
              id={id}
              onSubmit={form.handleSubmit(handleSubmit)}
              className="space-y-4"
            >
              <FormField
                control={form.control}
                name="title"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Title *</FormLabel>
                    <FormControl>
                      <Input {...field} placeholder="Task title" />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="description"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Description</FormLabel>
                    <FormControl>
                      <Textarea
                        {...field}
                        rows={5}
                        placeholder="Task description"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </form>
          </Form>
          <DialogFooter className="pt-4">
            <Button type="submit" form={id} disabled={mutation.isPending}>
              {mutation.isPending && (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              )}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { TaskActions };
