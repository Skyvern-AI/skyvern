import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePostHog } from "posthog-js/react";
import { useGoogleSheetTabs } from "@/hooks/useGoogleSheetTabs";
import { useCreateGoogleSheetTab } from "@/hooks/useCreateGoogleSheetTab";
import { useCurrentOrgId } from "@/hooks/useCurrentOrgId";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";
import { isReconnectRequired } from "@/util/googleSheetsErrors";
import {
  describeAxiosError,
  type SheetsBlockType,
} from "@/util/sheetsTelemetry";
import { InlineCreateRow } from "./InlineCreateRow";

type Props = {
  nodeId: string;
  credentialId: string;
  hasSelectedAccount: boolean;
  spreadsheetUrl: string;
  value: string;
  placeholder?: string;
  allowCreate: boolean;
  blockType: SheetsBlockType;
  onChange: (value: string) => void;
  onSelect: (tabName: string) => void;
};

function SheetTabCombobox({
  nodeId,
  credentialId,
  hasSelectedAccount,
  spreadsheetUrl,
  value,
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

  const isTypeable =
    hasSelectedAccount &&
    Boolean(credentialId) &&
    !isTemplateExpression(credentialId) &&
    !isTemplateExpression(spreadsheetUrl) &&
    extractSpreadsheetIdFromUrl(spreadsheetUrl) !== null &&
    !isTemplateExpression(value);

  const handleChange = (nextValue: string) => {
    onChange(nextValue);
  };

  const handleFocus = () => {
    if (isTypeable) {
      setIsOpen(true);
    }
  };

  return (
    <Popover open={isOpen && isTypeable} onOpenChange={setIsOpen}>
      <PopoverAnchor asChild>
        <div ref={anchorRef} className="relative">
          <WorkflowBlockInputTextarea
            nodeId={nodeId}
            value={value}
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
        className="nopan w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-border bg-slate-elevation1 p-0 shadow-lg"
      >
        <SheetTabListPanel
          credentialId={credentialId}
          spreadsheetUrl={spreadsheetUrl}
          filter={value}
          allowCreate={allowCreate}
          blockType={blockType}
          onPick={(name, tabIndex) => {
            postHog?.capture("sheets.tab.selected", {
              org_id: orgId,
              block_type: blockType,
              tab_index: tabIndex,
            });
            onSelect(name);
            setIsOpen(false);
          }}
        />
      </PopoverContent>
    </Popover>
  );
}

type ListPanelProps = {
  credentialId: string;
  spreadsheetUrl: string;
  filter: string;
  allowCreate: boolean;
  blockType: SheetsBlockType;
  onPick: (tabName: string, tabIndex: number) => void;
};

function SheetTabListPanel({
  credentialId,
  spreadsheetUrl,
  filter,
  allowCreate,
  blockType,
  onPick,
}: ListPanelProps) {
  const postHog = usePostHog();
  const orgId = useCurrentOrgId();
  const errorReportedRef = useRef<unknown>(null);
  const loadedReportedRef = useRef(false);
  const tabsQuery = useGoogleSheetTabs({
    credentialId,
    spreadsheetUrlOrId: spreadsheetUrl,
    enabled: true,
  });
  const createMutation = useCreateGoogleSheetTab();

  // Reset dedup refs when the underlying spreadsheet/credential changes so
  // telemetry reflects the new selection rather than a single per-mount fire.
  useEffect(() => {
    errorReportedRef.current = null;
    loadedReportedRef.current = false;
  }, [credentialId, spreadsheetUrl]);

  useEffect(() => {
    if (!tabsQuery.error) {
      errorReportedRef.current = null;
    } else if (errorReportedRef.current !== tabsQuery.error) {
      errorReportedRef.current = tabsQuery.error;
      const meta = describeAxiosError(tabsQuery.error);
      postHog?.capture("sheets.tab.error", {
        org_id: orgId,
        block_type: blockType,
        error_code: meta.error_code,
        http_status: meta.http_status,
      });
      return;
    }
    if (!tabsQuery.isFetching && tabsQuery.data && !loadedReportedRef.current) {
      loadedReportedRef.current = true;
      postHog?.capture("sheets.tab.loaded", {
        org_id: orgId,
        block_type: blockType,
        tab_count: tabsQuery.data.length,
      });
    }
  }, [
    tabsQuery.error,
    tabsQuery.data,
    tabsQuery.isFetching,
    postHog,
    orgId,
    blockType,
  ]);

  const tabs = useMemo(() => {
    const list = tabsQuery.data ?? [];
    const needle = filter.trim().toLowerCase();
    const filtered = needle
      ? list.filter((t) => t.title.toLowerCase().includes(needle))
      : list;
    return [...filtered].sort((a, b) =>
      a.title.localeCompare(b.title, undefined, { sensitivity: "base" }),
    );
  }, [tabsQuery.data, filter]);

  if (isReconnectRequired(tabsQuery.error)) {
    return (
      <div className="w-full p-3 text-xs">
        <p className="mb-2 text-foreground">
          Reconnect this Google account to enable the sheet picker.
        </p>
        <a
          href="/integrations"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-muted-foreground underline hover:text-foreground"
        >
          Open integrations <ExternalLinkIcon className="size-3" />
        </a>
      </div>
    );
  }

  const handleCreate = async (title: string) => {
    const tab = await createMutation.mutateAsync({
      credentialId,
      spreadsheetUrlOrId: spreadsheetUrl,
      title,
    });
    onPick(tab.title, -1);
  };

  return (
    <>
      {allowCreate ? (
        <div className="border-b border-border">
          <InlineCreateRow
            label="Create new sheet"
            placeholder="Sheet title"
            isSubmitting={createMutation.isPending}
            onConfirm={handleCreate}
          />
        </div>
      ) : null}
      <div className="max-h-[240px] overflow-y-auto">
        {tabsQuery.isLoading ? (
          <div className="space-y-1 px-3 py-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-4 w-2/3" />
            ))}
          </div>
        ) : tabs.length === 0 ? (
          <div className="px-3 py-3 text-xs text-muted-foreground">
            No sheets.
          </div>
        ) : (
          tabs.map((tab, index) => (
            <button
              key={tab.sheet_id}
              type="button"
              onClick={() => onPick(tab.title, index)}
              className="flex w-full px-3 py-2 text-left text-xs text-foreground hover:bg-slate-elevation4"
            >
              {tab.title}
            </button>
          ))
        )}
      </div>
    </>
  );
}

export { SheetTabCombobox };
