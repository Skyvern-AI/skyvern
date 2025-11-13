import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useDeleteBrowserProfileMutation } from "./hooks/useDeleteBrowserProfileMutation";

type DeleteBrowserProfileButtonProps = {
  profileId: string;
  disabled?: boolean;
};

function DeleteBrowserProfileButton({
  profileId,
  disabled = false,
}: DeleteBrowserProfileButtonProps) {
  const deleteMutation = useDeleteBrowserProfileMutation();

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          size="sm"
          variant="destructive"
          disabled={disabled || deleteMutation.isPending}
        >
          Delete
        </Button>
      </DialogTrigger>
      <DialogContent onCloseAutoFocus={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Delete browser profile?</DialogTitle>
          <DialogDescription>
            This will remove the browser profile. Existing workflow runs will be
            unaffected, but you won&apos;t be able to reuse this profile unless
            it is recreated.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={deleteMutation.isPending}
            onClick={() => deleteMutation.mutate(profileId)}
          >
            {deleteMutation.isPending ? "Deleting…" : "Delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteBrowserProfileButton };
