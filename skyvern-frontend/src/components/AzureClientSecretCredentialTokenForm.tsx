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
} from "@/components/ui/form";
import { useAzureClientCredentialToken } from "@/hooks/useAzureClientCredentialToken";
import { EyeOpenIcon, EyeClosedIcon } from "@radix-ui/react-icons";

const AzureClientSecretCredentialSchema = z
  .object({
    tenant_id: z.string().min(1, "tenant_id is required"),
    client_id: z.string().min(1, "client_id is required"),
    client_secret: z.string().min(1, "client_secret is required"),
  })
  .strict();

const formSchema = z
  .object({
    credential: AzureClientSecretCredentialSchema,
  })
  .strict();

type FormData = z.infer<typeof formSchema>;

export function AzureClientSecretCredentialTokenForm() {
  const [showClientSecret, setShowClientSecret] = useState(false);
  const {
    azureOrganizationAuthToken,
    isLoading,
    createOrUpdateToken,
    isUpdating,
  } = useAzureClientCredentialToken();

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      credential: azureOrganizationAuthToken?.credential || {
        tenant_id: "",
        client_id: "",
        client_secret: "",
      },
    },
  });

  const onSubmit = (data: FormData) => {
    createOrUpdateToken(data);
  };

  const toggleClientSecretVisibility = () => {
    setShowClientSecret((v) => !v);
  };

  useEffect(() => {
    if (azureOrganizationAuthToken?.credential) {
      form.reset({ credential: azureOrganizationAuthToken.credential });
    }
  }, [azureOrganizationAuthToken, form]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-medium">
            Azure Client Secret Credential
          </h3>
          <p className="text-sm text-muted-foreground">
            Configure your Azure Client Secret Credential to give access to your
            Azure account.
          </p>
        </div>
        {azureOrganizationAuthToken && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Status:</span>
            <span
              className={`text-sm ${azureOrganizationAuthToken.valid ? "text-green-600" : "text-red-600"}`}
            >
              {azureOrganizationAuthToken.valid ? "Active" : "Inactive"}
            </span>
          </div>
        )}
      </div>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <FormField
            control={form.control}
            name="credential.tenant_id"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Tenant ID</FormLabel>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="tenant_id"
                      disabled={isLoading || isUpdating}
                    />
                  </FormControl>
                </div>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="credential.client_id"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Client ID</FormLabel>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="client_id"
                      disabled={isLoading || isUpdating}
                    />
                  </FormControl>
                </div>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="credential.client_secret"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Client Secret</FormLabel>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type={showClientSecret ? "text" : "password"}
                      placeholder="client_secret"
                      disabled={isLoading || isUpdating}
                    />
                  </FormControl>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                    onClick={toggleClientSecretVisibility}
                    disabled={isLoading || isUpdating}
                  >
                    {showClientSecret ? (
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
              {isUpdating ? "Updating..." : "Update Credential"}
            </Button>
            {azureOrganizationAuthToken && (
              <div className="text-sm text-muted-foreground">
                Last updated:{" "}
                {new Date(
                  azureOrganizationAuthToken.modified_at,
                ).toLocaleDateString()}
              </div>
            )}
          </div>
        </form>
      </Form>

      {azureOrganizationAuthToken && (
        <div className="rounded-md bg-muted p-4">
          <h4 className="mb-2 text-sm font-medium">Credential Information</h4>
          <div className="space-y-1 text-sm text-muted-foreground">
            <div>ID: {azureOrganizationAuthToken.id}</div>
            <div>Type: {azureOrganizationAuthToken.token_type}</div>
            <div>
              Created:{" "}
              {new Date(
                azureOrganizationAuthToken.created_at,
              ).toLocaleDateString()}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
