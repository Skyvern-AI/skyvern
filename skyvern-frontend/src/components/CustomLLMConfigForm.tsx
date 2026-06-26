import { useEffect, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import * as z from "zod";
import {
  CheckIcon,
  Cross2Icon,
  EyeClosedIcon,
  EyeOpenIcon,
  Pencil1Icon,
  PlusIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { CustomLLM, CustomLLMConfig, CustomLLMProvider } from "@/api/types";
import { useCustomLLMs } from "@/hooks/useCustomLLMs";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

const providerLabels: Record<CustomLLMProvider, string> = {
  openai_compatible: "OpenAI compatible",
  ollama: "Ollama",
  openrouter: "OpenRouter",
};

const optionalString = z.preprocess(
  (value) => (typeof value === "string" ? value.trim() || null : value),
  z.string().nullable().optional(),
);

const optionalNumber = (schema: z.ZodNumber) =>
  z.preprocess((value) => {
    if (value === "" || value === null || value === undefined) {
      return null;
    }
    return Number(value);
  }, schema.nullable().optional());

const configSchema = z
  .object({
    display_name: z.string().trim().min(1, "Name is required"),
    provider: z.enum(["openai_compatible", "ollama", "openrouter"]),
    model_name: z.string().trim().min(1, "Model ID is required"),
    api_base: optionalString,
    api_key: optionalString,
    api_version: optionalString,
    supports_vision: z.boolean(),
    add_assistant_prefix: z.boolean(),
    max_completion_tokens: optionalNumber(
      z
        .number()
        .int("Max tokens must be a whole number")
        .min(1, "Max tokens must be at least 1")
        .max(1_000_000, "Max tokens must be 1,000,000 or less"),
    ),
    temperature: optionalNumber(
      z
        .number()
        .min(0, "Temperature must be at least 0")
        .max(2, "Temperature must be 2 or less"),
    ),
    reasoning_effort: optionalString,
  })
  .superRefine((value, ctx) => {
    if (value.provider === "openai_compatible" && !value.api_base) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["api_base"],
        message: "API base is required",
      });
    }
    if (
      (value.provider === "openai_compatible" ||
        value.provider === "openrouter") &&
      !value.api_key
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["api_key"],
        message: "API key is required",
      });
    }
  });

const formSchema = z.object({ config: configSchema });

type FormData = z.infer<typeof formSchema>;

const providerDefaults: Record<CustomLLMProvider, Partial<CustomLLMConfig>> = {
  openai_compatible: {
    api_base: "",
    api_key: "",
    api_version: "",
    supports_vision: true,
    add_assistant_prefix: false,
  },
  ollama: {
    api_base: "http://localhost:11434",
    api_key: "",
    api_version: "",
    supports_vision: false,
    add_assistant_prefix: false,
  },
  openrouter: {
    api_base: "https://openrouter.ai/api/v1",
    api_key: "",
    api_version: "",
    supports_vision: true,
    add_assistant_prefix: false,
  },
};

function emptyFormValues(provider: CustomLLMProvider = "openai_compatible") {
  return {
    config: {
      display_name: "",
      provider,
      model_name: "",
      api_base: providerDefaults[provider].api_base ?? "",
      api_key: providerDefaults[provider].api_key ?? "",
      api_version: providerDefaults[provider].api_version ?? "",
      supports_vision: providerDefaults[provider].supports_vision ?? true,
      add_assistant_prefix:
        providerDefaults[provider].add_assistant_prefix ?? false,
      max_completion_tokens: null,
      temperature: null,
      reasoning_effort: "",
    },
  };
}

function valuesForCustomLLM(customLLM: CustomLLM): FormData {
  return {
    config: {
      ...customLLM.config,
      api_base: customLLM.config.api_base ?? "",
      api_key: customLLM.config.api_key ?? "",
      api_version: customLLM.config.api_version ?? "",
      max_completion_tokens: customLLM.config.max_completion_tokens ?? null,
      temperature: customLLM.config.temperature ?? null,
      reasoning_effort: customLLM.config.reasoning_effort ?? "",
    },
  };
}

function requestFromValues(values: FormData): { config: CustomLLMConfig } {
  return {
    config: {
      ...values.config,
      api_base: values.config.api_base || null,
      api_key: values.config.api_key || null,
      api_version: values.config.api_version || null,
      max_completion_tokens: values.config.max_completion_tokens || null,
      temperature:
        values.config.temperature === undefined
          ? null
          : values.config.temperature,
      reasoning_effort: values.config.reasoning_effort || null,
    },
  };
}

export function CustomLLMConfigForm() {
  const [editing, setEditing] = useState<CustomLLM | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const {
    customLLMs,
    isLoading,
    createCustomLLM,
    updateCustomLLM,
    deleteCustomLLM,
    isCreating,
    isUpdating,
    isDeleting,
  } = useCustomLLMs();

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: emptyFormValues(),
  });
  const provider = form.watch("config.provider");
  const isMutating = isCreating || isUpdating || isDeleting;

  useEffect(() => {
    if (!editing) {
      return;
    }
    form.reset(valuesForCustomLLM(editing));
  }, [editing, form]);

  const resetForm = () => {
    setEditing(null);
    setShowApiKey(false);
    form.reset(emptyFormValues());
  };

  const onSubmit = (values: FormData) => {
    const request = requestFromValues(values);
    if (editing) {
      updateCustomLLM({ id: editing.id, request }, { onSuccess: resetForm });
      return;
    }
    createCustomLLM(request, { onSuccess: resetForm });
  };

  const onProviderChange = (nextProvider: CustomLLMProvider) => {
    form.setValue("config.provider", nextProvider);
    form.setValue(
      "config.api_base",
      providerDefaults[nextProvider].api_base ?? "",
    );
    form.setValue(
      "config.api_key",
      providerDefaults[nextProvider].api_key ?? "",
    );
    form.setValue(
      "config.api_version",
      providerDefaults[nextProvider].api_version ?? "",
    );
    form.setValue(
      "config.supports_vision",
      providerDefaults[nextProvider].supports_vision ?? true,
    );
    form.setValue(
      "config.add_assistant_prefix",
      providerDefaults[nextProvider].add_assistant_prefix ?? false,
    );
  };

  return (
    <div className="space-y-6">
      {customLLMs.length > 0 && (
        <div className="space-y-2">
          {customLLMs.map((customLLM) => (
            <div
              key={customLLM.id}
              className="flex flex-col gap-3 rounded-md border p-3 md:flex-row md:items-center md:justify-between"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {customLLM.config.display_name}
                  </span>
                  <span className="rounded border px-2 py-0.5 text-xs text-muted-foreground">
                    {providerLabels[customLLM.config.provider]}
                  </span>
                </div>
                <div className="break-all text-xs text-muted-foreground">
                  {customLLM.config.model_name}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={isMutating}
                  onClick={() => setEditing(customLLM)}
                >
                  <Pencil1Icon className="mr-2 h-4 w-4" />
                  Edit
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="destructive"
                  disabled={isMutating}
                  onClick={() => deleteCustomLLM(customLLM)}
                >
                  <TrashIcon className="mr-2 h-4 w-4" />
                  Delete
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
          <div className="grid gap-4 md:grid-cols-2">
            <FormField
              control={form.control}
              name="config.display_name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Name</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      placeholder="Local Llama"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="config.provider"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Provider</FormLabel>
                  <Select
                    value={field.value}
                    onValueChange={(value) =>
                      onProviderChange(value as CustomLLMProvider)
                    }
                    disabled={isLoading || isMutating}
                  >
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Provider" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {Object.entries(providerLabels).map(([value, label]) => (
                        <SelectItem key={value} value={value}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>

          <FormField
            control={form.control}
            name="config.model_name"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Model ID</FormLabel>
                <FormControl>
                  <Input
                    {...field}
                    placeholder={
                      provider === "openrouter"
                        ? "anthropic/claude-3.5-sonnet"
                        : provider === "ollama"
                          ? "llama3.1"
                          : "mistral"
                    }
                    disabled={isLoading || isMutating}
                  />
                </FormControl>
                <FormDescription>
                  {provider === "ollama"
                    ? "Use the Ollama model name. Prefixes like ollama_chat/ are accepted."
                    : "Use the provider model identifier without the LiteLLM prefix."}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="grid gap-4 md:grid-cols-2">
            <FormField
              control={form.control}
              name="config.api_base"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>API Base</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      value={field.value ?? ""}
                      placeholder="https://api.example.com/v1"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="config.api_key"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    API Key{provider === "ollama" ? " (optional)" : ""}
                  </FormLabel>
                  <div className="relative">
                    <FormControl>
                      <Input
                        {...field}
                        value={field.value ?? ""}
                        type={showApiKey ? "text" : "password"}
                        placeholder={
                          provider === "ollama" ? "optional" : "sk-..."
                        }
                        disabled={isLoading || isMutating}
                      />
                    </FormControl>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                      onClick={() => setShowApiKey((value) => !value)}
                      disabled={isLoading || isMutating}
                    >
                      {showApiKey ? (
                        <EyeClosedIcon className="h-4 w-4" />
                      ) : (
                        <EyeOpenIcon className="h-4 w-4" />
                      )}
                    </Button>
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            {provider === "openai_compatible" && (
              <FormField
                control={form.control}
                name="config.api_version"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>API Version</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        value={field.value ?? ""}
                        placeholder="optional"
                        disabled={isLoading || isMutating}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            )}

            <FormField
              control={form.control}
              name="config.max_completion_tokens"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Max Tokens</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      value={field.value ?? ""}
                      type="number"
                      min={1}
                      placeholder="default"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="config.temperature"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Temperature</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      value={field.value ?? ""}
                      type="number"
                      min={0}
                      max={2}
                      step="0.1"
                      placeholder="default"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="config.reasoning_effort"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Reasoning Effort</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      value={field.value ?? ""}
                      placeholder="optional"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>

          <div className="flex flex-wrap gap-6">
            <FormField
              control={form.control}
              name="config.supports_vision"
              render={({ field }) => (
                <FormItem className="flex items-center gap-2 space-y-0">
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormLabel className="text-sm font-normal">
                    Supports vision
                  </FormLabel>
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="config.add_assistant_prefix"
              render={({ field }) => (
                <FormItem className="flex items-center gap-2 space-y-0">
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormLabel className="text-sm font-normal">
                    Assistant prefix
                  </FormLabel>
                </FormItem>
              )}
            />
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={isLoading || isMutating}>
              {editing ? (
                <CheckIcon className="mr-2 h-4 w-4" />
              ) : (
                <PlusIcon className="mr-2 h-4 w-4" />
              )}
              {editing
                ? isUpdating
                  ? "Saving..."
                  : "Save Custom LLM"
                : isCreating
                  ? "Adding..."
                  : "Add Custom LLM"}
            </Button>
            {editing && (
              <Button
                type="button"
                variant="outline"
                disabled={isLoading || isMutating}
                onClick={resetForm}
              >
                <Cross2Icon className="mr-2 h-4 w-4" />
                Cancel
              </Button>
            )}
          </div>
        </form>
      </Form>
    </div>
  );
}
