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
        title: "인증 정보 삭제됨",
        variant: "success",
        description: "인증 정보가 삭제되었습니다.",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "인증 정보 삭제 실패",
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
          <TooltipContent>인증 정보 삭제</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>확실합니까?</DialogTitle>
        </DialogHeader>
        <div className="text-sm text-slate-400">
          인증 정보{" "}
          <span className="font-bold text-primary">{credential.name}</span>이(가)
          영구적으로 삭제됩니다. 삭제된 인증 정보는 복구할 수 없습니다.
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">취소</Button>
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
            삭제
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteCredentialButton };
