import { DialogClose } from "@/components/ui/dialog";
import {
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
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
import { ReloadIcon, TrashIcon } from "@radix-ui/react-icons";
import { CredentialApiResponse } from "@/api/types";
type Props = {
  credential: CredentialApiResponse;
};

function DeleteCredentialButton({ credential }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const deleteCredentialMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/credentials/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
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
    <Dialog>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>
              <Button size="icon" variant="tertiary" className="h-8 w-9">
                <TrashIcon className="size-5" />
              </Button>
            </DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>Delete Credential</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Are you sure?</DialogTitle>
        </DialogHeader>
        <div className="text-sm text-slate-400">
          The credential{" "}
          <span className="font-bold text-primary">{credential.name}</span> will
          be PERMANENTLY deleted. The Skyvern team has no way to restore a
          credential once it's deleted.
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => {
              deleteCredentialMutation.mutate(credential.credential_id);
            }}
            disabled={deleteCredentialMutation.isPending}
          >
            {deleteCredentialMutation.isPending && (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            )}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteCredentialButton };
