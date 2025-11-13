import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { basicLocalTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { useBrowserProfileQuery } from "./hooks/useBrowserProfileQuery";

type BrowserProfileDetailsDialogProps = {
  profileId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

function BrowserProfileDetailsDialog({
  profileId,
  open,
  onOpenChange,
}: BrowserProfileDetailsDialogProps) {
  const { data: profile, isLoading } = useBrowserProfileQuery(profileId);

  const isDeleted = Boolean(profile?.deleted_at);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Browser Profile
            {isDeleted ? <Badge variant="secondary">Deleted</Badge> : null}
          </DialogTitle>
        </DialogHeader>
        {isLoading ? (
          <div className="space-y-4">
            <Skeleton className="h-6 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : profile ? (
          <ScrollArea className="max-h-96">
            <div className="space-y-4 pr-4 text-sm">
              <div className="space-y-1">
                <Label className="text-xs uppercase text-slate-400">Name</Label>
                <div className="font-medium">{profile.name}</div>
              </div>

              <div className="space-y-1">
                <Label className="text-xs uppercase text-slate-400">
                  Browser Profile ID
                </Label>
                <div className="flex items-center gap-2 font-mono text-xs text-slate-500">
                  <span className="truncate">{profile.browser_profile_id}</span>
                  <CopyText text={profile.browser_profile_id} />
                </div>
              </div>

              <div className="space-y-1">
                <Label className="text-xs uppercase text-slate-400">
                  Description
                </Label>
                <div
                  className={cn(
                    !profile.description && "italic text-slate-400",
                  )}
                >
                  {profile.description ?? "No description provided."}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <Label className="text-xs uppercase text-slate-400">
                    Created
                  </Label>
                  <div>{basicLocalTimeFormat(profile.created_at)}</div>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs uppercase text-slate-400">
                    Updated
                  </Label>
                  <div>{basicLocalTimeFormat(profile.modified_at)}</div>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs uppercase text-slate-400">
                    Organization
                  </Label>
                  <div className="font-mono text-xs text-slate-500">
                    {profile.organization_id}
                  </div>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs uppercase text-slate-400">
                    Deleted
                  </Label>
                  <div>
                    {profile.deleted_at
                      ? basicLocalTimeFormat(profile.deleted_at)
                      : "Not deleted"}
                  </div>
                </div>
              </div>
            </div>
          </ScrollArea>
        ) : (
          <div className="text-sm text-slate-500">
            Unable to load browser profile details.
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export { BrowserProfileDetailsDialog };

