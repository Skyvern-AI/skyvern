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
import { ClearCredentialDialog } from "@/components/ClearCredentialDialog";
import { useBitwardenCredential } from "@/hooks/useBitwardenCredential";
import { EyeOpenIcon, EyeClosedIcon } from "@radix-ui/react-icons";

const BitwardenCredentialSchema = z
  .object({
    email: z
      .string()
      .min(1, "Email is required")
      .email("Must be a valid email"),
    master_password: z.string().min(1, "Master password is required"),
    client_id: z.string().trim().optional(),
    client_secret: z.string().trim().optional(),
  })
  .strict()
  .superRefine(({ client_id, client_secret }, ctx) => {
    if (Boolean(client_id) !== Boolean(client_secret)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Enter both the client ID and the client secret of your Bitwarden personal API key, or neither.",
        path: [client_id ? "client_secret" : "client_id"],
      });
    }
    if (client_id && !client_id.startsWith("user.")) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Use a Bitwarden personal API key. Its client ID starts with 'user.'.",
        path: ["client_id"],
      });
    }
  });

const formSchema = z
  .object({
    credential: BitwardenCredentialSchema,
  })
  .strict();

type FormData = z.infer<typeof formSchema>;

type Props = {
  onSuccess?: () => void;
};

export function BitwardenCredentialForm({ onSuccess }: Props = {}) {
  const [showMasterPassword, setShowMasterPassword] = useState(false);
  const [showClientSecret, setShowClientSecret] = useState(false);
  const {
    bitwardenOrganizationAuthToken,
    isLoading,
    createOrUpdateToken,
    isUpdating,
    clearCredential,
    isClearing,
  } = useBitwardenCredential();

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      credential: {
        email: bitwardenOrganizationAuthToken?.credential?.email || "",
        master_password: "",
        client_id: bitwardenOrganizationAuthToken?.credential?.client_id || "",
        client_secret: "",
      },
    },
  });
  const isMutating = isUpdating || isClearing;

  const onSubmit = (data: FormData) => {
    const { client_id, client_secret, ...credential } = data.credential;
    createOrUpdateToken(
      {
        credential: {
          ...credential,
          ...(client_id && client_secret ? { client_id, client_secret } : {}),
        },
      },
      {
        onSuccess: () => {
          form.reset({
            credential: {
              email: credential.email,
              master_password: "",
              client_id: client_id && client_secret ? client_id : "",
              client_secret: "",
            },
          });
          onSuccess?.();
        },
      },
    );
  };

  useEffect(() => {
    form.reset({
      credential: {
        email: bitwardenOrganizationAuthToken?.credential?.email || "",
        master_password: "",
        client_id: bitwardenOrganizationAuthToken?.credential?.client_id || "",
        client_secret: "",
      },
    });
  }, [
    bitwardenOrganizationAuthToken?.credential?.email,
    bitwardenOrganizationAuthToken?.credential?.client_id,
    form,
  ]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-medium">Bitwarden Credential</h3>
          <p className="text-sm text-muted-foreground">
            Configure your Bitwarden credentials to give access to your
            Bitwarden vault.
          </p>
        </div>
        {bitwardenOrganizationAuthToken && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Status:</span>
            <span
              className={`text-sm ${bitwardenOrganizationAuthToken.valid ? "text-green-600" : "text-red-600"}`}
            >
              {bitwardenOrganizationAuthToken.valid ? "Active" : "Inactive"}
            </span>
          </div>
        )}
      </div>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <FormField
            control={form.control}
            name="credential.email"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Email</FormLabel>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type="email"
                      placeholder="user@example.com"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                </div>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="credential.master_password"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Master Password</FormLabel>
                <div className="relative">
                  <FormControl>
                    <Input
                      {...field}
                      type={showMasterPassword ? "text" : "password"}
                      placeholder="master_password"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                    onClick={() => setShowMasterPassword((v) => !v)}
                    disabled={isLoading || isMutating}
                  >
                    {showMasterPassword ? (
                      <EyeClosedIcon className="h-4 w-4" />
                    ) : (
                      <EyeOpenIcon className="h-4 w-4" />
                    )}
                  </Button>
                </div>
                <FormMessage />
                {bitwardenOrganizationAuthToken && (
                  <p className="text-xs text-muted-foreground">
                    Master password is not displayed for security. Enter it
                    again to update your credential.
                  </p>
                )}
              </FormItem>
            )}
          />

          <section className="space-y-4">
            <div>
              <h4 className="text-sm font-medium">
                Personal API key (optional)
              </h4>
              <p className="text-sm text-muted-foreground">
                Use this if Bitwarden asks Skyvern for a verification code (a
                new-device check or two-step login). Find the key in the
                Bitwarden web app under Settings &gt; Security &gt; Keys &gt;
                View API key. Skyvern still uses your master password to unlock
                the vault.
              </p>
            </div>
            <FormField
              control={form.control}
              name="credential.client_id"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Client ID</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="user.…"
                      disabled={isLoading || isMutating}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="credential.client_secret"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Client secret</FormLabel>
                  <div className="relative">
                    <FormControl>
                      <Input
                        {...field}
                        type={showClientSecret ? "text" : "password"}
                        disabled={isLoading || isMutating}
                      />
                    </FormControl>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                      onClick={() => setShowClientSecret((v) => !v)}
                      disabled={isLoading || isMutating}
                    >
                      {showClientSecret ? (
                        <EyeClosedIcon className="h-4 w-4" />
                      ) : (
                        <EyeOpenIcon className="h-4 w-4" />
                      )}
                    </Button>
                  </div>
                  <FormMessage />
                  {bitwardenOrganizationAuthToken?.credential?.client_id && (
                    <p className="text-xs text-muted-foreground">
                      Client secret is not displayed for security. Enter it
                      again to keep using the API key.
                    </p>
                  )}
                </FormItem>
              )}
            />
          </section>

          <div className="flex items-center gap-4">
            <Button type="submit" disabled={isLoading || isMutating}>
              {isUpdating ? "Updating..." : "Update Credential"}
            </Button>
            {bitwardenOrganizationAuthToken && (
              <ClearCredentialDialog
                label="Clear Credential"
                title="Clear Bitwarden credential?"
                description="Workflows that use Bitwarden-backed credentials will no longer be able to resolve them until new Bitwarden credentials are added."
                disabled={isLoading || isMutating}
                isPending={isClearing}
                onConfirm={() => clearCredential()}
              />
            )}
            {bitwardenOrganizationAuthToken && (
              <div className="text-sm text-muted-foreground">
                Last updated:{" "}
                {new Date(
                  bitwardenOrganizationAuthToken.modified_at,
                ).toLocaleDateString()}
              </div>
            )}
          </div>
        </form>
      </Form>

      {bitwardenOrganizationAuthToken && (
        <div className="rounded-md bg-muted p-4">
          <h4 className="mb-2 text-sm font-medium">Credential Information</h4>
          <div className="space-y-1 text-sm text-muted-foreground">
            <div>ID: {bitwardenOrganizationAuthToken.id}</div>
            <div>Type: {bitwardenOrganizationAuthToken.token_type}</div>
            <div>
              Created:{" "}
              {new Date(
                bitwardenOrganizationAuthToken.created_at,
              ).toLocaleDateString()}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
