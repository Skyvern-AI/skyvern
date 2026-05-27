import { useState } from "react";
import { ChevronLeftIcon, Pencil1Icon } from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

import { DeleteBrowserProfileButton } from "./DeleteBrowserProfileButton";
import { RenameBrowserProfileDialog } from "./RenameBrowserProfileDialog";
import { useBrowserProfileQuery } from "./hooks/useBrowserProfileQuery";

function BrowserProfileDetailPage() {
  const navigate = useNavigate();
  const { profileId } = useParams<{ profileId: string }>();
  const [renameOpen, setRenameOpen] = useState(false);

  const {
    data: profile,
    isLoading,
    isError,
  } = useBrowserProfileQuery(profileId);

  return (
    <div className="space-y-5">
      <div>
        <Button
          variant="tertiary"
          size="sm"
          onClick={() => navigate("/browser-profiles")}
        >
          <ChevronLeftIcon className="mr-1 size-4" />
          Browser Profiles
        </Button>
      </div>

      {isLoading && (
        <div className="space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-32 w-full" />
        </div>
      )}

      {!isLoading && (isError || !profile) && (
        <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-neutral-600 dark:text-slate-300">
          Browser profile not found.
        </div>
      )}

      {!isLoading && profile && (
        <>
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-2">
              <h1 className="text-2xl">{profile.name}</h1>
              {profile.description && (
                <p className="text-sm text-neutral-600 dark:text-slate-300">
                  {profile.description}
                </p>
              )}
              {profile.deleted_at && (
                <p className="text-sm text-amber-400">
                  Deleted {basicLocalTimeFormat(profile.deleted_at)}
                </p>
              )}
            </div>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                onClick={() => setRenameOpen(true)}
                disabled={Boolean(profile.deleted_at)}
              >
                <Pencil1Icon className="mr-2 size-4" />
                Rename
              </Button>
              {!profile.deleted_at && (
                <DeleteBrowserProfileButton
                  profile={profile}
                  onDeleted={() => navigate("/browser-profiles")}
                />
              )}
            </div>
          </div>

          <div className="rounded-lg border border-slate-700 bg-slate-elevation1 p-6">
            <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-[180px_1fr]">
              <dt className="text-sm text-neutral-600 dark:text-slate-400">
                ID
              </dt>
              <dd className="flex items-center font-mono text-sm">
                <span className="break-all">{profile.browser_profile_id}</span>
                <CopyText
                  className="ml-1 opacity-75 hover:opacity-100"
                  text={profile.browser_profile_id}
                />
              </dd>

              <dt className="text-sm text-neutral-600 dark:text-slate-400">
                Source Browser
              </dt>
              <dd className="text-sm">
                {profile.source_browser_type ?? (
                  <span className="opacity-50">—</span>
                )}
              </dd>

              <dt className="text-sm text-neutral-600 dark:text-slate-400">
                Created
              </dt>
              <dd
                className="text-sm"
                title={basicTimeFormat(profile.created_at)}
              >
                {basicLocalTimeFormat(profile.created_at)}
              </dd>

              <dt className="text-sm text-neutral-600 dark:text-slate-400">
                Last Modified
              </dt>
              <dd
                className="text-sm"
                title={basicTimeFormat(profile.modified_at)}
              >
                {basicLocalTimeFormat(profile.modified_at)}
              </dd>
            </dl>
          </div>

          <RenameBrowserProfileDialog
            profile={profile}
            open={renameOpen}
            onOpenChange={setRenameOpen}
          />
        </>
      )}
    </div>
  );
}

export { BrowserProfileDetailPage };
