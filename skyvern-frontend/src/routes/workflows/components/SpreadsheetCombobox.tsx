import { ExternalLinkIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePostHog } from "posthog-js/react";
import { useDebounce } from "use-debounce";
import { useGoogleSpreadsheets } from "@/hooks/useGoogleSpreadsheets";
import { useCreateGoogleSpreadsheet } from "@/hooks/useCreateGoogleSpreadsheet";
import { useCurrentOrgId } from "@/hooks/useCurrentOrgId";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { handleInfiniteScroll } from "@/util/utils";
import {
  buildSpreadsheetUrl,
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";
import { isReconnectRequired } from "@/util/googleSheetsErrors";
import {
  describeAxiosError,
  hashSpreadsheetId,
  type SheetsBlockType,
} from "@/util/sheetsTelemetry";
import { InlineCreateRow } from "./InlineCreateRow";

type Selection = {
  url: string;
  name: string;
  firstSheetName: string | null;
};

type Props = {
  nodeId: string;
  credentialId: string;
  hasSelectedAccount: boolean;
  value: string;
  displayName: string | null;
  placeholder?: string;
  allowCreate: boolean;
  blockType: SheetsBlockType;
  onChange: (value: string) => void;
  onSelect: (selection: Selection) => void;
};

function SpreadsheetCombobox({
  nodeId,
  credentialId,
  hasSelectedAccount,
  value,
  displayName,
  placeholder,
  allowCreate,
  blockType,
  onChange,
  onSelect,
}: Props) {
  const [isOpen, setIsOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement>(null);
  const postHog = usePostHog();
  const orgId = useCurrentOrgId();
  const renderedValue = displayName ?? value;
  // Skip the Drive search whenever we already have a resolved selection
  // (displayName) or the input is a parseable URL/ID. Drive `q` searches by
  // title, so URLs and post-selection titles only generate wasted requests.
  const queryForSearch =
    displayName || extractSpreadsheetIdFromUrl(renderedValue)
      ? ""
      : renderedValue;
  const [debouncedQuery] = useDebounce(queryForSearch, 300);

  const isTypeable =
    hasSelectedAccount &&
    Boolean(credentialId) &&
    !isTemplateExpression(credentialId) &&
    !isTemplateExpression(value);

  const handleChange = (nextValue: string) => {
    onChange(nextValue);
  };

  const handleFocus = () => {
    if (isTypeable) {
      setIsOpen(true);
    }
  };

  const popoverOpen = isOpen && isTypeable;
  useEffect(() => {
    if (popoverOpen) {
      postHog?.capture("sheets.spreadsheet.picker.opened", {
        org_id: orgId,
        block_type: blockType,
      });
    }
  }, [popoverOpen, postHog, orgId, blockType]);

  const handlePick = (selection: Selection) => {
    onSelect(selection);
    setIsOpen(false);
    // Hash off the hot path so a slow SubtleCrypto call never blocks the UI.
    const spreadsheetId = extractSpreadsheetIdFromUrl(selection.url);
    void (
      spreadsheetId ? hashSpreadsheetId(spreadsheetId) : Promise.resolve(null)
    ).then((spreadsheetIdHash) => {
      postHog?.capture("sheets.spreadsheet.picker.selected", {
        org_id: orgId,
        block_type: blockType,
        spreadsheet_id_hash: spreadsheetIdHash,
      });
    });
  };

  return (
    <Popover open={isOpen && isTypeable} onOpenChange={setIsOpen}>
      <PopoverAnchor asChild>
        <div ref={anchorRef} className="relative">
          <WorkflowBlockInputTextarea
            nodeId={nodeId}
            value={renderedValue}
            onChange={handleChange}
            onFocus={handleFocus}
            placeholder={placeholder}
            hideActions={!hasSelectedAccount}
            className="nopan text-xs"
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
        className="nopan w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-slate-700 bg-slate-900 p-0 shadow-lg"
      >
        <SpreadsheetListPanel
          credentialId={credentialId}
          query={debouncedQuery}
          allowCreate={allowCreate}
          blockType={blockType}
          onPick={handlePick}
        />
      </PopoverContent>
    </Popover>
  );
}

type ListPanelProps = {
  credentialId: string;
  query: string;
  allowCreate: boolean;
  blockType: SheetsBlockType;
  onPick: (selection: Selection) => void;
};

function SpreadsheetListPanel({
  credentialId,
  query,
  allowCreate,
  blockType,
  onPick,
}: ListPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const postHog = usePostHog();
  const orgId = useCurrentOrgId();
  const errorReportedRef = useRef<unknown>(null);
  const listing = useGoogleSpreadsheets({
    credentialId,
    query,
    enabled: true,
  });
  const createMutation = useCreateGoogleSpreadsheet();

  const items = useMemo(
    () => listing.data?.pages.flatMap((p) => p.spreadsheets) ?? [],
    [listing.data],
  );

  // Reset dedup so a new credential gets a fresh error window.
  useEffect(() => {
    errorReportedRef.current = null;
  }, [credentialId]);

  useEffect(() => {
    if (!listing.error) {
      errorReportedRef.current = null;
      return;
    }
    if (errorReportedRef.current === listing.error) return;
    errorReportedRef.current = listing.error;
    const meta = describeAxiosError(listing.error);
    postHog?.capture("sheets.spreadsheet.picker.error", {
      org_id: orgId,
      block_type: blockType,
      error_code: meta.error_code,
      http_status: meta.http_status,
    });
  }, [listing.error, postHog, orgId, blockType]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => e.stopPropagation();
    el.addEventListener("wheel", handler, { passive: true });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  if (isReconnectRequired(listing.error)) {
    return (
      <div className="w-full p-3 text-xs">
        <p className="mb-2 text-slate-200">
          Reconnect this Google account to enable the sheet picker.
        </p>
        <a
          href="/integrations"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-slate-300 underline hover:text-slate-100"
        >
          Open integrations <ExternalLinkIcon className="size-3" />
        </a>
      </div>
    );
  }

  const handleCreate = async (title: string) => {
    const result = await createMutation.mutateAsync({ credentialId, title });
    const { spreadsheet, first_sheet_name } = result;
    onPick({
      url: spreadsheet.web_view_link ?? buildSpreadsheetUrl(spreadsheet.id),
      name: spreadsheet.name,
      firstSheetName: first_sheet_name,
    });
  };

  return (
    <>
      {allowCreate ? (
        <div className="border-b border-slate-700">
          <InlineCreateRow
            label="Create new spreadsheet"
            placeholder="Spreadsheet title"
            isSubmitting={createMutation.isPending}
            onConfirm={handleCreate}
          />
        </div>
      ) : null}
      <div
        ref={scrollRef}
        className="max-h-[280px] overflow-y-auto"
        onScroll={(e) =>
          handleInfiniteScroll(
            e,
            listing.fetchNextPage,
            listing.hasNextPage ?? false,
            listing.isFetchingNextPage,
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
        ) : listing.error ? (
          <div className="px-3 py-3 text-xs text-amber-200">
            Could not load spreadsheets. Please try again.
          </div>
        ) : items.length === 0 ? (
          <div className="px-3 py-3 text-xs text-slate-500">
            No spreadsheets found.
          </div>
        ) : (
          <>
            {items.map((ss) => (
              <button
                key={ss.id}
                type="button"
                onClick={() =>
                  onPick({
                    url: buildSpreadsheetUrl(ss.id),
                    name: ss.name,
                    firstSheetName: null,
                  })
                }
                className="flex w-full flex-col gap-0.5 px-3 py-2 text-left text-xs hover:bg-slate-700"
              >
                <span className="font-medium text-slate-200">{ss.name}</span>
                {ss.modified_time ? (
                  <span className="text-slate-500">
                    Modified {new Date(ss.modified_time).toLocaleString()}
                  </span>
                ) : null}
              </button>
            ))}
            {listing.isFetchingNextPage ? (
              <div className="flex items-center justify-center py-2">
                <ReloadIcon className="size-3 animate-spin text-slate-400" />
              </div>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}

export { SpreadsheetCombobox };
