import { useState } from "react";
import { PlusIcon, ReloadIcon } from "@radix-ui/react-icons";

import { PINNED_RESIDENTIAL_ISP_PROXY_LOCATION } from "@/api/types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { useCreateBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation";
import { useBrowserProfileCreateStore } from "@/store/useBrowserProfileCreateStore";

type Props = {
  size?: "default" | "lg";
  label?: string;
};

function CreateBrowserProfileButton({
  size = "default",
  label = "Create a Browser Profile",
}: Props) {
  const [open, setOpen] = useState(false);
  const [pinResidentialIspProxy, setPinResidentialIspProxy] = useState(false);
  const createBrowserSessionMutation = useCreateBrowserSessionMutation();
  const isBackgroundCreateInProgress = useBrowserProfileCreateStore(
    (state) => state.active !== null,
  );

  const disabled =
    createBrowserSessionMutation.isPending || isBackgroundCreateInProgress;

  const handleCreate = () => {
    createBrowserSessionMutation.mutate({
      proxyLocation: pinResidentialIspProxy
        ? PINNED_RESIDENTIAL_ISP_PROXY_LOCATION
        : null,
      proxySessionId: null,
      timeout: null,
      generateBrowserProfile: true,
    });
    setOpen(false);
  };

  return (
    <>
      <Button
        size={size}
        disabled={disabled}
        title={
          isBackgroundCreateInProgress
            ? "A browser profile is already being created"
            : undefined
        }
        onClick={() => setOpen(true)}
      >
        {createBrowserSessionMutation.isPending ? (
          <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <PlusIcon className="mr-2 h-4 w-4" />
        )}
        {label}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent onCloseAutoFocus={(event) => event.preventDefault()}>
          <DialogHeader>
            <DialogTitle>Create Browser Profile</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="flex items-center gap-3">
              <Checkbox
                id="create-browser-profile-pin-residential-isp-proxy"
                checked={pinResidentialIspProxy}
                onCheckedChange={(checked) =>
                  setPinResidentialIspProxy(checked === true)
                }
              />
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <Label
                    htmlFor="create-browser-profile-pin-residential-isp-proxy"
                    className="cursor-pointer text-sm font-medium"
                  >
                    Use a consistent IP address
                  </Label>
                  <HelpTooltip content="Starts the capture browser on a stable residential IP so the saved login is less likely to be challenged later." />
                </div>
                <p className="text-xs leading-5 text-muted-foreground">
                  Skyvern will create a residential IP identity for this
                  profile.
                </p>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button type="button" onClick={handleCreate}>
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export { CreateBrowserProfileButton };
