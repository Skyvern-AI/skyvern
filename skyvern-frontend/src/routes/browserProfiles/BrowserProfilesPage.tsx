import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { basicLocalTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { useBrowserProfilesQuery } from "./hooks/useBrowserProfilesQuery";
import { CreateBrowserProfileDrawer } from "./CreateBrowserProfileDrawer";
import { BrowserProfileDetailsDialog } from "./BrowserProfileDetailsDialog";
import { DeleteBrowserProfileButton } from "./DeleteBrowserProfileButton";

function BrowserProfilesPage() {
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(
    null,
  );
  const [includeDeleted, setIncludeDeleted] = useState(false);

  const { data: profiles, isLoading } = useBrowserProfilesQuery({
    includeDeleted,
  });

  const hasProfiles = (profiles?.length ?? 0) > 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold">Browser Profiles</h1>
          <p className="text-sm text-slate-400">
            Create, manage, and reuse browser state across workflow runs.
          </p>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="include-deleted"
              checked={includeDeleted}
              onCheckedChange={(checked) => setIncludeDeleted(checked === true)}
            />
            <Label htmlFor="include-deleted" className="text-sm">
              Show deleted
            </Label>
          </div>
          <Button onClick={() => setIsCreateOpen(true)}>New Profile</Button>
        </div>
      </div>

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/4">Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="w-1/6">Created</TableHead>
              <TableHead className="w-1/6">Updated</TableHead>
              <TableHead className="w-32 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              Array.from({ length: 5 }).map((_, idx) => (
                <TableRow key={idx}>
                  <TableCell colSpan={5}>
                    <Skeleton className="h-9 w-full" />
                  </TableCell>
                </TableRow>
              ))
            ) : !hasProfiles ? (
              <TableRow>
                <TableCell colSpan={5}>
                  <div className="py-6 text-center text-sm text-slate-400">
                    {includeDeleted
                      ? "No browser profiles found (including deleted)."
                      : "No browser profiles yet. Create one to reuse browser state across runs."}
                  </div>
                </TableCell>
              </TableRow>
            ) : (
              profiles?.map((profile) => {
                const isDeleted = profile.deleted_at !== null;

                return (
                  <TableRow
                    key={profile.browser_profile_id}
                    className={cn(isDeleted && "opacity-70")}
                  >
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{profile.name}</span>
                        {isDeleted ? (
                          <Badge variant="secondary">Deleted</Badge>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-lg truncate">
                      {profile.description ?? "—"}
                    </TableCell>
                    <TableCell title={profile.created_at}>
                      {basicLocalTimeFormat(profile.created_at)}
                    </TableCell>
                    <TableCell title={profile.modified_at}>
                      {basicLocalTimeFormat(profile.modified_at)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() =>
                            setSelectedProfileId(profile.browser_profile_id)
                          }
                        >
                          View
                        </Button>
                        <DeleteBrowserProfileButton
                          profileId={profile.browser_profile_id}
                          disabled={isDeleted}
                        />
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      <CreateBrowserProfileDrawer
        open={isCreateOpen}
        onOpenChange={setIsCreateOpen}
      />
      <BrowserProfileDetailsDialog
        profileId={selectedProfileId}
        open={selectedProfileId !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedProfileId(null);
          }
        }}
      />
    </div>
  );
}

export { BrowserProfilesPage };

