import { useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { TrashIcon } from "@radix-ui/react-icons";
import { CredentialApiResponse } from "@/api/types";
type Props = {
  credential: CredentialApiResponse;
};

function DeleteCredentialButton({ credential }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const deleteCredentialMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/credentials/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      setOpen(false);
      toast({
        title: "Credential deleted",
        variant: "success",
        description: "The credential has been deleted.",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete credential",
        description: error.message,
      });
    },
  });

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              size="icon"
              variant="tertiary"
              className="h-8 w-9"
              onClick={() => setOpen(true)}
            >
              <TrashIcon className="size-5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Delete Credential</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete credential?"
        description={
          <p>
            The credential{" "}
            <span className="font-bold text-primary">{credential.name}</span>{" "}
            will be permanently deleted.
          </p>
        }
        reversibilityNote="The Skyvern team can't restore a credential once it's deleted."
        isPending={deleteCredentialMutation.isPending}
        onConfirm={() =>
          deleteCredentialMutation.mutate(credential.credential_id)
        }
      />
    </>
  );
}

export { DeleteCredentialButton };
