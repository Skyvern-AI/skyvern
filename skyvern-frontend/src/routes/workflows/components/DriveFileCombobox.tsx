import { ExternalLinkIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useDebounce } from "use-debounce";
import {
  DrivePicker,
  DrivePickerDocsView,
} from "@googleworkspace/drive-picker-react";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useGoogleDriveFiles } from "@/hooks/useGoogleDriveFiles";
import {
  getGoogleOAuthCredentialScopesGranted,
  hasGoogleOAuthCredentialScopes,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  GOOGLE_DRIVE_REQUIRED_SCOPES,
  isNarrowDriveGrant,
} from "@/util/googleScopes";
import { isReconnectRequired } from "@/util/googleSheetsErrors";
import { isTemplateExpression } from "@/util/googleSheetsUrl";
import {
  driveIdFromValue,
  driveItemUrl,
  drivePickerConfig,
  drivePickerUnavailableReason,
  fetchDrivePickerToken,
} from "@/util/googleDrivePicker";
import { cn, handleInfiniteScroll } from "@/util/utils";
import { TemplateExpressionRow } from "./TemplateExpressionRow";

const FOLDER_MIME = "application/vnd.google-apps.folder";

type Kind = "file" | "folder";

type PickedEvent = { detail: { docs?: Array<{ id?: string }> } };

type Props = {
  nodeId: string;
  name: string;
  credentialId: string;
  kind: Kind;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
};

function DriveFileCombobox({
  nodeId,
  name,
  credentialId,
  kind,
  value,
  onChange,
  placeholder,
  className,
  ...ariaProps
}: Props) {
  const [isOpen, setIsOpen] = useState(false);
  const [templateMode, setTemplateMode] = useState(false);
  const [picker, setPicker] = useState<{
    token: string;
    fileIds?: string;
  } | null>(null);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const anchorRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const { credentials } = useGoogleOAuthCredentials();

  const account = credentials.find((c) => c.id === credentialId);
  const wholeDrive = account
    ? hasGoogleOAuthCredentialScopes(account, GOOGLE_DRIVE_REQUIRED_SCOPES)
    : null;
  const effectiveTemplateMode =
    templateMode ||
    isTemplateExpression(value) ||
    isTemplateExpression(credentialId);
  const canPick = Boolean(credentialId) && !effectiveTemplateMode;
  const pastedId = effectiveTemplateMode ? null : driveIdFromValue(value);

  const [debouncedQuery] = useDebounce(pastedId ? "" : value.trim(), 300);
  const searchQuery = isTemplateExpression(debouncedQuery)
    ? ""
    : debouncedQuery;

  // Only accounts limited to chosen files need to know whether a pasted link is already granted.
  const grantedCheckEnabled =
    canPick && wholeDrive === false && Boolean(pastedId);
  const grantedListing = useGoogleDriveFiles({
    credentialId,
    folders: kind === "folder",
    query: "",
    enabled: grantedCheckEnabled,
  });
  const pastedIdGranted =
    grantedListing.data?.pages.some((p) =>
      p.files.some((f) => f.id === pastedId),
    ) ?? false;
  const {
    hasNextPage: grantedHasNextPage,
    isFetchingNextPage: grantedFetchingNextPage,
    isFetchNextPageError: grantedNextPageFailed,
    fetchNextPage: fetchNextGrantedPage,
  } = grantedListing;
  useEffect(() => {
    if (
      !grantedCheckEnabled ||
      pastedIdGranted ||
      !grantedHasNextPage ||
      grantedFetchingNextPage ||
      grantedNextPageFailed
    ) {
      return;
    }
    void fetchNextGrantedPage();
  }, [
    grantedCheckEnabled,
    pastedIdGranted,
    grantedHasNextPage,
    grantedFetchingNextPage,
    grantedNextPageFailed,
    fetchNextGrantedPage,
  ]);
  const showGiveAccess =
    grantedCheckEnabled &&
    grantedListing.isSuccess &&
    !grantedHasNextPage &&
    !pastedIdGranted;
  const grantedCheckFailed =
    grantedCheckEnabled &&
    grantedNextPageFailed &&
    !grantedFetchingNextPage &&
    !pastedIdGranted;

  const openPicker = (fileId?: string) => {
    setIsOpen(false);
    setPickerError(null);
    fetchDrivePickerToken(credentialId)
      .then((token) => setPicker({ token, fileIds: fileId }))
      .catch((error: unknown) =>
        setPickerError(
          error instanceof Error ? error.message : "Could not open Drive.",
        ),
      );
  };

  const handlePicked = (event: PickedEvent) => {
    const id = event.detail.docs?.[0]?.id;
    setPicker(null);
    if (!id) return;
    onChange(driveItemUrl(kind, id));
    void queryClient.invalidateQueries({ queryKey: ["googleDrive", "files"] });
  };

  return (
    <div className="space-y-1">
      <Popover open={isOpen && canPick} onOpenChange={setIsOpen}>
        <PopoverAnchor asChild>
          <div ref={anchorRef} className="relative">
            <WorkflowBlockInputTextarea
              name={name}
              nodeId={nodeId}
              value={value}
              onChange={(next) => {
                if (templateMode && !next) setTemplateMode(false);
                onChange(next);
              }}
              onFocus={() => canPick && setIsOpen(true)}
              placeholder={
                effectiveTemplateMode
                  ? kind === "folder"
                    ? "{{ target_folder_url }}"
                    : "{{ source_file_url }}"
                  : placeholder
              }
              className={cn("nopan text-xs", className)}
              {...ariaProps}
            />
          </div>
        </PopoverAnchor>
        <PopoverContent
          align="start"
          sideOffset={4}
          onOpenAutoFocus={(e) => e.preventDefault()}
          onInteractOutside={(e) => {
            if (anchorRef.current?.contains(e.target as Node)) {
              e.preventDefault();
            }
          }}
          className="nopan w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-border bg-slate-elevation1 p-0 shadow-lg"
        >
          <DriveListPanel
            credentialId={credentialId}
            kind={kind}
            query={searchQuery}
            onPick={(id) => {
              onChange(driveItemUrl(kind, id));
              setIsOpen(false);
            }}
          />
          <div className="border-t border-border">
            <button
              type="button"
              disabled={drivePickerUnavailableReason !== null}
              onClick={() => openPicker()}
              className="flex w-full flex-col gap-0.5 px-3 py-2 text-left text-xs text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent dark:text-slate-200 dark:hover:bg-slate-700"
            >
              <span>Choose more from Drive…</span>
              {drivePickerUnavailableReason ? (
                <span className="text-muted-foreground dark:text-slate-400">
                  {drivePickerUnavailableReason}
                </span>
              ) : null}
            </button>
          </div>
          <TemplateExpressionRow
            onClick={() => {
              setIsOpen(false);
              setTemplateMode(true);
            }}
          />
        </PopoverContent>
      </Popover>

      {showGiveAccess || grantedCheckFailed || wholeDrive !== null ? (
        <div className="flex items-start justify-between gap-2 text-xs">
          {showGiveAccess ? (
            <p className="text-muted-foreground dark:text-slate-400">
              This account opens only files chosen for it.{" "}
              <button
                type="button"
                disabled={drivePickerUnavailableReason !== null}
                onClick={() => openPicker(pastedId ?? undefined)}
                className="font-medium text-foreground underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60 dark:text-slate-200"
              >
                Give access in Drive
              </button>
              {drivePickerUnavailableReason ? (
                <> ({drivePickerUnavailableReason})</>
              ) : null}
            </p>
          ) : grantedCheckFailed ? (
            <p className="text-amber-700 dark:text-amber-200">
              Couldn't check whether this account can open this file.{" "}
              <button
                type="button"
                onClick={() => void fetchNextGrantedPage()}
                className="font-medium underline"
              >
                Retry
              </button>
            </p>
          ) : (
            <span />
          )}
          {wholeDrive !== null ? (
            <Badge
              variant="secondary"
              className="shrink-0 px-1.5 py-0.5 text-[11px]"
              title={
                wholeDrive
                  ? "This account can open every file in its Drive."
                  : "This account can open only files chosen for it and files its runs create."
              }
            >
              {wholeDrive ? "Whole Drive" : "Chosen files"}
            </Badge>
          ) : null}
        </div>
      ) : null}

      {pickerError ? (
        <p className="text-xs text-amber-700 dark:text-amber-200">
          {pickerError}
        </p>
      ) : null}

      {picker ? (
        <DrivePicker
          client-id={drivePickerConfig.clientId}
          app-id={drivePickerConfig.appId}
          developer-key={drivePickerConfig.apiKey}
          oauth-token={picker.token}
          onPicked={(e) => handlePicked(e as unknown as PickedEvent)}
          onCanceled={() => setPicker(null)}
        >
          <DrivePickerDocsView
            enable-drives={picker.fileIds ? undefined : "true"}
            include-folders="true"
            select-folder-enabled={kind === "folder" ? "true" : "false"}
            mime-types={kind === "folder" ? FOLDER_MIME : undefined}
            file-ids={picker.fileIds}
          />
        </DrivePicker>
      ) : null}
    </div>
  );
}

type ListPanelProps = {
  credentialId: string;
  kind: Kind;
  query: string;
  onPick: (id: string) => void;
};

function DriveListPanel({ credentialId, kind, query, onPick }: ListPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const listing = useGoogleDriveFiles({
    credentialId,
    folders: kind === "folder",
    query,
    enabled: true,
  });
  const items = useMemo(
    () => listing.data?.pages.flatMap((p) => p.files) ?? [],
    [listing.data],
  );
  const searchIncomplete =
    listing.data?.pages.some((p) => p.incomplete_search) ?? false;
  const {
    hasNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
    fetchNextPage,
  } = listing;

  // A short page leaves nothing to scroll, so keep fetching until the panel scrolls or the list ends.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !hasNextPage || isFetchingNextPage || isFetchNextPageError) {
      return;
    }
    if (el.scrollHeight <= el.clientHeight) {
      void fetchNextPage();
    }
  }, [
    items.length,
    hasNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
    fetchNextPage,
  ]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => e.stopPropagation();
    el.addEventListener("wheel", handler, { passive: true });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  const noun = kind === "folder" ? "folders" : "files";

  if (isReconnectRequired(listing.error)) {
    return (
      <div className="w-full p-3 text-xs">
        <p className="mb-2 text-foreground dark:text-slate-200">
          Reconnect this Google account to list its Drive {noun}.
        </p>
        <a
          href="/integrations"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-tertiary-foreground underline hover:text-foreground"
        >
          Open integrations <ExternalLinkIcon className="size-3" />
        </a>
      </div>
    );
  }

  return (
    <>
      <div
        ref={scrollRef}
        className="max-h-[280px] overflow-y-auto"
        onScroll={(e) =>
          handleInfiniteScroll(
            e,
            fetchNextPage,
            (hasNextPage ?? false) && !isFetchNextPageError,
            isFetchingNextPage,
          )
        }
      >
        {listing.isLoading ? (
          <div className="space-y-1 px-3 py-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="space-y-1">
                <Skeleton className="h-3.5 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
              </div>
            ))}
          </div>
        ) : listing.error && !isFetchNextPageError ? (
          <div className="px-3 py-3 text-xs text-amber-700 dark:text-amber-200">
            Could not load {noun}. Please try again.
          </div>
        ) : (
          <>
            {items.length === 0 && !hasNextPage && !isFetchNextPageError ? (
              <div className="px-3 py-3 text-xs text-muted-foreground dark:text-slate-500">
                {query
                  ? `No ${noun} match "${query}".`
                  : `No ${noun} this account can open yet.`}
              </div>
            ) : null}
            {items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onPick(item.id)}
                className="flex w-full flex-col gap-0.5 px-3 py-2 text-left text-xs hover:bg-muted dark:hover:bg-slate-700"
              >
                <span className="font-medium text-foreground dark:text-slate-200">
                  {item.name}
                </span>
                {item.modified_time ? (
                  <span className="text-muted-foreground dark:text-slate-500">
                    Modified {new Date(item.modified_time).toLocaleString()}
                  </span>
                ) : null}
              </button>
            ))}
            {isFetchNextPageError && !isFetchingNextPage ? (
              <button
                type="button"
                onClick={() => void fetchNextPage()}
                className="w-full px-3 py-2 text-left text-xs text-amber-700 hover:bg-muted dark:text-amber-200 dark:hover:bg-slate-700"
              >
                Could not load more {noun}. Retry
              </button>
            ) : null}
            {isFetchingNextPage ? (
              <div className="flex items-center justify-center py-2">
                <ReloadIcon className="size-3 animate-spin text-muted-foreground" />
              </div>
            ) : null}
          </>
        )}
      </div>
      {searchIncomplete ? (
        <div className="border-t px-3 py-2 text-xs text-muted-foreground dark:text-slate-500">
          Some shared drives could not be searched. If a{" "}
          {kind === "folder" ? "folder" : "file"} is missing, paste its link.
        </div>
      ) : null}
    </>
  );
}

function DriveFileInput(props: Props) {
  const { credentials } = useGoogleOAuthCredentials();
  const account = credentials.find((c) => c.id === props.credentialId);
  if (
    account &&
    isNarrowDriveGrant(getGoogleOAuthCredentialScopesGranted(account))
  ) {
    return <DriveFileCombobox {...props} />;
  }
  return (
    <WorkflowBlockInputTextarea
      name={props.name}
      nodeId={props.nodeId}
      value={props.value}
      onChange={props.onChange}
      placeholder={props.placeholder}
      aria-invalid={props["aria-invalid"]}
      aria-describedby={props["aria-describedby"]}
      className={cn("nopan text-xs", props.className)}
    />
  );
}

export { DriveFileCombobox, DriveFileInput };
