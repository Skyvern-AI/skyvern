import {
  Cross2Icon,
  ExternalLinkIcon,
  FileIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useDebounce } from "use-debounce";
import {
  DrivePicker,
  DrivePickerDocsView,
} from "@googleworkspace/drive-picker-react";
import { isAxiosError } from "axios";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  useGoogleDriveFiles,
  useGoogleDriveItem,
} from "@/hooks/useGoogleDriveFiles";
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
  drivePickerConfig,
  drivePickerConfigured,
  drivePickerUnavailableReason,
  fetchDrivePickerToken,
} from "@/util/googleDrivePicker";
import { cn, handleInfiniteScroll } from "@/util/utils";
import { TemplateExpressionRow } from "./TemplateExpressionRow";

const FOLDER_MIME = "application/vnd.google-apps.folder";

type Kind = "file" | "folder" | "any";

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
  disabled?: boolean;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
};

type ListProps = Omit<Props, "kind" | "value" | "onChange"> & {
  value: string[];
  onChange: (value: string[]) => void;
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
  disabled,
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
  const canPick = Boolean(credentialId) && !disabled && !effectiveTemplateMode;
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
    if (disabled) return;
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
    onChange(id);
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
              onChange(id);
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
            select-folder-enabled={kind === "file" ? "false" : "true"}
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
  const narrowAccount = Boolean(
    account &&
    isNarrowDriveGrant(getGoogleOAuthCredentialScopesGranted(account)),
  );
  const [textMode, setTextMode] = useState(false);
  const [picker, setPicker] = useState<{
    credentialId: string;
    token: string;
  } | null>(null);
  const [pickerError, setPickerError] = useState<{
    credentialId: string;
    message: string;
  } | null>(null);
  const queryClient = useQueryClient();
  const itemId = driveIdFromValue(props.value);
  const item = useGoogleDriveItem(
    props.credentialId || undefined,
    drivePickerConfigured && narrowAccount && !isTemplateExpression(props.value)
      ? itemId
      : null,
  );
  const refusalMessage =
    pickerError?.credentialId === props.credentialId
      ? pickerError.message
      : null;
  const textInput =
    narrowAccount && props.kind !== "any" ? (
      <DriveFileCombobox {...props} />
    ) : (
      <WorkflowBlockInputTextarea
        name={props.name}
        nodeId={props.nodeId}
        value={props.value}
        onChange={props.onChange}
        placeholder={props.placeholder}
        disabled={props.disabled}
        aria-invalid={props["aria-invalid"]}
        aria-describedby={props["aria-describedby"]}
        className={cn("nopan text-xs", props.className)}
      />
    );

  const openPicker = () => {
    if (props.disabled) return;
    const credentialId = props.credentialId;
    setPickerError(null);
    fetchDrivePickerToken(credentialId)
      .then((token) => setPicker({ credentialId, token }))
      .catch((error: unknown) => {
        if (error instanceof Error && !isAxiosError(error)) {
          setPickerError({ credentialId, message: error.message });
        }
      });
  };

  const handlePicked = (event: PickedEvent) => {
    const id = event.detail.docs?.[0]?.id;
    setPicker(null);
    if (!id) return;
    props.onChange(id);
    void queryClient.invalidateQueries({
      queryKey: ["googleDrive", "item", props.credentialId, id],
    });
  };

  if (!drivePickerConfigured || !narrowAccount) {
    return textInput;
  }
  if (!props.credentialId || isTemplateExpression(props.value)) {
    return textInput;
  }
  if (refusalMessage) {
    return (
      <div className="space-y-1">
        {textInput}
        <div className="flex items-center gap-2 text-xs text-muted-foreground dark:text-slate-400">
          <span>{refusalMessage}</span>
          <button
            type="button"
            disabled={props.disabled}
            onClick={openPicker}
            className="shrink-0 font-medium underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }
  if (textMode || (props.value !== "" && itemId === null)) {
    return textInput;
  }

  const pickerElement =
    picker?.credentialId === props.credentialId ? (
      <DrivePicker
        client-id={drivePickerConfig.clientId}
        app-id={drivePickerConfig.appId}
        developer-key={drivePickerConfig.apiKey}
        oauth-token={picker.token}
        onPicked={(event) => handlePicked(event as unknown as PickedEvent)}
        onCanceled={() => setPicker(null)}
      >
        <DrivePickerDocsView
          enable-drives="true"
          include-folders="true"
          select-folder-enabled={props.kind === "file" ? "false" : "true"}
          mime-types={props.kind === "folder" ? FOLDER_MIME : undefined}
        />
      </DrivePicker>
    ) : null;

  if (!itemId) {
    return (
      <div className="space-y-1">
        <Button
          type="button"
          variant="outline"
          disabled={props.disabled}
          onClick={openPicker}
          className={cn("w-full", props.className)}
        >
          Choose from Google Drive
        </Button>
        <button
          type="button"
          disabled={props.disabled}
          onClick={() => setTextMode(true)}
          className="text-xs text-muted-foreground underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60 dark:text-slate-400"
        >
          Paste a link or use an input instead
        </button>
        {pickerElement}
      </div>
    );
  }

  const isFolder = item.data?.is_folder ?? props.kind === "folder";
  const notFound =
    item.isError &&
    isAxiosError(item.error) &&
    item.error.response?.status === 404;
  return (
    <div className="space-y-1">
      <div
        aria-invalid={props["aria-invalid"]}
        aria-describedby={props["aria-describedby"]}
        className={cn(
          "flex min-h-9 items-center gap-2 rounded-md border border-input bg-transparent px-3 py-1 text-xs",
          props["aria-invalid"] && "border-destructive",
          props.className,
        )}
      >
        {isFolder ? (
          <FolderIcon className="size-4 shrink-0" />
        ) : (
          <FileIcon className="size-4 shrink-0" />
        )}
        <span className="min-w-0 flex-1 truncate" title={props.value}>
          {item.data?.name ?? props.value}
        </span>
        <button
          type="button"
          disabled={props.disabled}
          onClick={openPicker}
          className="font-medium underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60"
        >
          Change
        </button>
      </div>
      <button
        type="button"
        disabled={props.disabled}
        onClick={() => setTextMode(true)}
        className="text-xs text-muted-foreground underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60 dark:text-slate-400"
      >
        Edit as text
      </button>
      {notFound ? (
        <p className="text-xs text-muted-foreground dark:text-slate-400">
          Not found in this account
        </p>
      ) : null}
      {pickerElement}
    </div>
  );
}

function DriveFileListItem({
  credentialId,
  value,
  disabled,
  onRemove,
}: {
  credentialId: string;
  value: string;
  disabled?: boolean;
  onRemove: () => void;
}) {
  const itemId = driveIdFromValue(value);
  const item = useGoogleDriveItem(
    credentialId || undefined,
    drivePickerConfigured && !isTemplateExpression(value) ? itemId : null,
  );
  const label = item.data?.name ?? value;
  const isFolder = item.data?.is_folder ?? value.includes("/folders/");
  const notFound =
    item.isError &&
    isAxiosError(item.error) &&
    item.error.response?.status === 404;

  return (
    <div className="rounded-md border border-input bg-transparent px-2 py-1.5 text-xs">
      <div className="flex min-w-0 items-center gap-2">
        {isFolder ? (
          <FolderIcon className="size-4 shrink-0" />
        ) : (
          <FileIcon className="size-4 shrink-0" />
        )}
        <span className="min-w-0 flex-1 truncate" title={value}>
          {label}
        </span>
        <button
          type="button"
          disabled={disabled}
          onClick={onRemove}
          aria-label={`Remove ${label}`}
          className="shrink-0 text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Cross2Icon className="size-3.5" />
        </button>
      </div>
      {notFound ? (
        <p className="mt-1 text-muted-foreground dark:text-slate-400">
          Not found in this account
        </p>
      ) : null}
    </div>
  );
}

function DriveFileListInput(props: ListProps) {
  const { credentials } = useGoogleOAuthCredentials();
  const account = credentials.find((c) => c.id === props.credentialId);
  const narrowAccount = Boolean(
    account &&
    isNarrowDriveGrant(getGoogleOAuthCredentialScopesGranted(account)),
  );
  const [textMode, setTextMode] = useState(false);
  const [picker, setPicker] = useState<{
    credentialId: string;
    token: string;
  } | null>(null);
  const [pickerError, setPickerError] = useState<{
    credentialId: string;
    message: string;
  } | null>(null);
  const queryClient = useQueryClient();
  const refusalMessage =
    pickerError?.credentialId === props.credentialId
      ? pickerError.message
      : null;
  const textInput = (
    <WorkflowBlockInputTextarea
      name={props.name}
      nodeId={props.nodeId}
      value={props.value.join("\n")}
      onChange={(text) =>
        props.onChange(
          text
            .split(/\r?\n/)
            .map((value) => value.trim())
            .filter(Boolean),
        )
      }
      placeholder={props.placeholder}
      disabled={props.disabled}
      aria-invalid={props["aria-invalid"]}
      aria-describedby={props["aria-describedby"]}
      className={cn("nopan text-xs", props.className)}
    />
  );

  const openPicker = () => {
    if (props.disabled) return;
    const credentialId = props.credentialId;
    setPickerError(null);
    fetchDrivePickerToken(credentialId)
      .then((token) => setPicker({ credentialId, token }))
      .catch((error: unknown) => {
        if (error instanceof Error && !isAxiosError(error)) {
          setPickerError({ credentialId, message: error.message });
        }
      });
  };

  const handlePicked = (event: PickedEvent) => {
    const ids = event.detail.docs?.flatMap(({ id }) => (id ? [id] : [])) ?? [];
    setPicker(null);
    if (ids.length === 0) return;
    props.onChange(Array.from(new Set([...props.value, ...ids])));
    void queryClient.invalidateQueries({
      queryKey: ["googleDrive", "item", props.credentialId],
    });
  };

  if (!drivePickerConfigured || !narrowAccount) {
    return textInput;
  }
  if (refusalMessage) {
    return (
      <div className="space-y-1">
        {textInput}
        <div className="flex items-center gap-2 text-xs text-muted-foreground dark:text-slate-400">
          <span>{refusalMessage}</span>
          <button
            type="button"
            disabled={props.disabled}
            onClick={openPicker}
            className="shrink-0 font-medium underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  const pickerElement =
    picker?.credentialId === props.credentialId ? (
      <DrivePicker
        client-id={drivePickerConfig.clientId}
        app-id={drivePickerConfig.appId}
        developer-key={drivePickerConfig.apiKey}
        oauth-token={picker.token}
        multiselect
        onPicked={(event) => handlePicked(event as unknown as PickedEvent)}
        onCanceled={() => setPicker(null)}
      >
        <DrivePickerDocsView
          enable-drives="true"
          include-folders="true"
          select-folder-enabled="false"
        />
      </DrivePicker>
    ) : null;

  return (
    <div className="space-y-2">
      {textMode ? (
        textInput
      ) : (
        <div className="space-y-1">
          {props.value.map((value, index) => (
            <DriveFileListItem
              key={`${value}:${index}`}
              credentialId={props.credentialId}
              value={value}
              disabled={props.disabled}
              onRemove={() =>
                props.onChange(props.value.filter((_, item) => item !== index))
              }
            />
          ))}
        </div>
      )}
      <Button
        type="button"
        variant="outline"
        disabled={props.disabled}
        onClick={openPicker}
        className={cn("w-full", props.className)}
      >
        Choose from Google Drive
      </Button>
      {!textMode ? (
        <button
          type="button"
          disabled={props.disabled}
          onClick={() => setTextMode(true)}
          className="text-xs text-muted-foreground underline disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60 dark:text-slate-400"
        >
          Paste a link or use an input instead
        </button>
      ) : null}
      {pickerElement}
    </div>
  );
}

export { DriveFileCombobox, DriveFileInput, DriveFileListInput };
