import { useEffect, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";

import { BrowserProfileApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { useUpdateBrowserProfileMutation } from "./hooks/useBrowserProfileMutations";

type Props = {
  profile: BrowserProfileApiResponse;
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

function RenameBrowserProfileDialog({ profile, open, onOpenChange }: Props) {
  const [name, setName] = useState(profile.name);
  const updateProfileMutation = useUpdateBrowserProfileMutation();

  useEffect(() => {
    if (open) {
      setName(profile.name);
    }
  }, [open, profile.name]);

  const trimmedName = name.trim();
  const canSubmit =
    trimmedName.length > 0 &&
    trimmedName !== profile.name &&
    !updateProfileMutation.isPending;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    await updateProfileMutation.mutateAsync({
      profileId: profile.browser_profile_id,
      name: trimmedName,
    });
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Rename browser profile</DialogTitle>
          <DialogDescription>
            Give this browser profile a new name.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="browser-profile-name">Name</Label>
              <Input
                id="browser-profile-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {updateProfileMutation.isPending && (
                <ReloadIcon className="mr-2 size-4 animate-spin" />
              )}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export { RenameBrowserProfileDialog };
