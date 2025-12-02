import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
  FormDescription,
} from "@/components/ui/form";
import { useCustomCredentialServiceConfig } from "@/hooks/useCustomCredentialServiceConfig";
import { EyeOpenIcon, EyeClosedIcon, GlobeIcon } from "@radix-ui/react-icons";

const CustomCredentialServiceConfigSchema = z
  .object({
    api_base_url: z
      .string()
      .min(1, "API Base URL is required")
      .url("Must be a valid URL"),
    api_token: z.string().min(1, "API Token is required"),
  })
  .strict();

const formSchema = z
  .object({
    config: CustomCredentialServiceConfigSchema,
  })
  .strict();

type FormData = z.infer<typeof formSchema>;

export function CustomCredentialServiceConfigForm() {
  const [showApiToken, setShowApiToken] = useState(false);
  const {
    customCredentialServiceAuthToken,
    parsedConfig,
    isLoading,
    createOrUpdateConfig,
    isUpdating,
  } = useCustomCredentialServiceConfig();

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      config: parsedConfig || {
        api_base_url: "",
        api_token: "",
      },
    },
  });

  const onSubmit = (data: FormData) => {
    createOrUpdateConfig(data);
  };

  const toggleApiTokenVisibility = () => {
    setShowApiToken((v) => !v);
  };

  useEffect(() => {
    if (parsedConfig) {
      form.reset({ config: parsedConfig });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parsedConfig]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-medium">Custom Credential Service</h3>
          <p className="text-sm text-muted-foreground">
            Configure your custom HTTP API for credential management. Your API
            should support the standard CRUD operations.
          </p>
        </div>
        {customCredentialServiceAuthToken && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Status:</span>
            <span
              className={`text-sm ${customCredentialServiceAuthToken.valid ? "text-green-600" : "text-red-600"}`}
            >
              {customCredentialServiceAuthToken.valid ? "Active" : "Inactive"}
            </span>
          </div>
        )}
      </div>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <FormField
            control={form.control}
            name="config.api_base_url"
            render={({ field }) => (
              <FormItem>
                <FormLabel>API Base URL</FormLabel>
                <FormDescription>
                  The base URL of your custom credential service API (e.g.,
                  https://credentials.company.com/api/v1)
                </FormDescription>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type="url"
                      placeholder="https://credentials.company.com/api/v1"
                      disabled={isLoading || isUpdating}
                    />
                  </FormControl>
                  <GlobeIcon className="absolute right-3 top-3 h-4 w-4 text-muted-foreground" />
                </div>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="config.api_token"
            render={({ field }) => (
              <FormItem>
                <FormLabel>API Token</FormLabel>
                <FormDescription>
                  Bearer token for authenticating with your custom credential
                  service
                </FormDescription>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type={showApiToken ? "text" : "password"}
                      placeholder="your_api_token_here"
                      disabled={isLoading || isUpdating}
                    />
                  </FormControl>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                    onClick={toggleApiTokenVisibility}
                    disabled={isLoading || isUpdating}
                  >
                    {showApiToken ? (
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

          <div className="flex items-center gap-4">
            <Button type="submit" disabled={isLoading || isUpdating}>
              {isUpdating ? "Updating..." : "Update Configuration"}
            </Button>

            {customCredentialServiceAuthToken && (
              <div className="text-sm text-muted-foreground">
                Last updated:{" "}
                {new Date(
                  customCredentialServiceAuthToken.modified_at,
                ).toLocaleDateString()}
              </div>
            )}
          </div>
        </form>
      </Form>

      {customCredentialServiceAuthToken && (
        <div className="rounded-md bg-muted p-4">
          <h4 className="mb-2 text-sm font-medium">
            Configuration Information
          </h4>
          <div className="space-y-1 text-sm text-muted-foreground">
            <div>ID: {customCredentialServiceAuthToken.id}</div>
            <div>Type: {customCredentialServiceAuthToken.token_type}</div>
            <div>
              Created:{" "}
              {new Date(
                customCredentialServiceAuthToken.created_at,
              ).toLocaleDateString()}
            </div>
            {parsedConfig && (
              <div className="mt-2">
                <div>
                  <strong>Configured API URL:</strong>{" "}
                  {parsedConfig.api_base_url}
                </div>
                <div>
                  <strong>Token (masked):</strong>{" "}
                  {parsedConfig.api_token.length > 8
                    ? `${parsedConfig.api_token.slice(0, 8)}...`
                    : "********"}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
