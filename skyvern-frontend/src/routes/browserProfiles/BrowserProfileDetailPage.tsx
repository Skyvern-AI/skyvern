import { useEffect, useState } from "react";
import {
  ChevronLeftIcon,
  Pencil1Icon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";

import { PINNED_RESIDENTIAL_ISP_PROXY_LOCATION } from "@/api/types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

import { DeleteBrowserProfileButton } from "./DeleteBrowserProfileButton";
import { RenameBrowserProfileDialog } from "./RenameBrowserProfileDialog";
import { useBrowserProfileQuery } from "./hooks/useBrowserProfileQuery";
import { useUpdateBrowserProfileMutation } from "./hooks/useBrowserProfileMutations";

function formatProxyIdentity(value?: string | null) {
  if (!value) {
    return null;
  }
  return `${value.slice(0, 3)}...${value.slice(-2)}`;
}

function BrowserProfileDetailPage() {
  const navigate = useNavigate();
  const { profileId } = useParams<{ profileId: string }>();
  const [renameOpen, setRenameOpen] = useState(false);
  const [pinResidentialIspProxy, setPinResidentialIspProxy] = useState(false);
  const [rotateProxyPin, setRotateProxyPin] = useState(false);
  const updateProfileMutation = useUpdateBrowserProfileMutation();

  const {
    data: profile,
    isLoading,
    isError,
  } = useBrowserProfileQuery(profileId);

  useEffect(() => {
    if (!profile) {
      return;
    }
    setPinResidentialIspProxy(Boolean(profile.proxy_session_id));
    setRotateProxyPin(false);
  }, [profile]);

  const existingProxyPinEnabled = Boolean(profile?.proxy_session_id);
  const existingProxyIdentity = formatProxyIdentity(profile?.proxy_session_id);
  const proxyPinChanged = Boolean(
    profile &&
    (pinResidentialIspProxy !== existingProxyPinEnabled ||
      (pinResidentialIspProxy && rotateProxyPin)),
  );

  const handleSaveProxyPin = () => {
    if (!profile || !proxyPinChanged) {
      return;
    }
    updateProfileMutation.mutate({
      profileId: profile.browser_profile_id,
      proxy_location: pinResidentialIspProxy
        ? PINNED_RESIDENTIAL_ISP_PROXY_LOCATION
        : null,
      proxy_session_id: pinResidentialIspProxy ? undefined : null,
      rotate_proxy_session_id:
        pinResidentialIspProxy && rotateProxyPin ? true : undefined,
    });
  };

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

            <div className="mt-6 border-t border-slate-700 pt-4">
              <div className="flex items-start gap-3">
                <Checkbox
                  id="browser-profile-pin-residential-isp-proxy"
                  checked={pinResidentialIspProxy}
                  onCheckedChange={(checked) =>
                    setPinResidentialIspProxy(checked === true)
                  }
                  disabled={Boolean(profile.deleted_at)}
                  className="mt-0.5"
                />
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Label
                      htmlFor="browser-profile-pin-residential-isp-proxy"
                      className="cursor-pointer text-sm font-medium"
                    >
                      Use a consistent IP address
                    </Label>
                    <HelpTooltip content="Routes browser sessions launched with this profile through the same residential IP to reduce account security prompts caused by changing IPs." />
                  </div>
                  <p className="text-xs leading-5 text-muted-foreground">
                    Helps profiles keep the same residential IP across launches,
                    so saved logins are less likely to be challenged.
                  </p>
                  {pinResidentialIspProxy &&
                    existingProxyIdentity &&
                    profile.proxy_session_id && (
                      <div className="space-y-2">
                        <p className="flex items-center gap-1 text-xs text-slate-300">
                          <span>
                            Consistent IP active: identity{" "}
                            {existingProxyIdentity}
                          </span>
                          <CopyText
                            className="opacity-75 hover:opacity-100"
                            text={profile.proxy_session_id}
                          />
                        </p>
                        <div className="flex flex-wrap items-center gap-2">
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
                            onClick={() => setRotateProxyPin(true)}
                            disabled={
                              Boolean(profile.deleted_at) || rotateProxyPin
                            }
                          >
                            Rotate IP identity
                          </Button>
                          {rotateProxyPin && (
                            <span className="text-xs text-slate-300">
                              A new IP identity will be created when you save.
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                  {pinResidentialIspProxy && !existingProxyIdentity && (
                    <p className="text-xs text-slate-300">
                      Skyvern will create an IP identity for this profile when
                      you save.
                    </p>
                  )}
                </div>
              </div>
              <div className="mt-4 flex justify-end">
                <Button
                  variant="secondary"
                  onClick={handleSaveProxyPin}
                  disabled={
                    !proxyPinChanged ||
                    updateProfileMutation.isPending ||
                    Boolean(profile.deleted_at)
                  }
                >
                  {updateProfileMutation.isPending && (
                    <ReloadIcon className="mr-2 size-4 animate-spin" />
                  )}
                  Save IP Settings
                </Button>
              </div>
            </div>
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
