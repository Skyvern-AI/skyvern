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
  })
  .strict();

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
      },
    },
  });
  const isMutating = isUpdating || isClearing;

  const onSubmit = (data: FormData) => {
    createOrUpdateToken(data, {
      onSuccess: () => onSuccess?.(),
    });
  };

  useEffect(() => {
    form.reset({
      credential: {
        email: bitwardenOrganizationAuthToken?.credential?.email || "",
        master_password: "",
      },
    });
  }, [bitwardenOrganizationAuthToken?.credential?.email, form]);

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
