import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { useMemo, useRef, useState } from "react";
import { useGoogleSheetTabs } from "@/hooks/useGoogleSheetTabs";
import { useCreateGoogleSheetTab } from "@/hooks/useCreateGoogleSheetTab";
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
import { InlineCreateRow } from "./InlineCreateRow";

type Props = {
  nodeId: string;
  credentialId: string;
  hasSelectedAccount: boolean;
  spreadsheetUrl: string;
  value: string;
  placeholder?: string;
  allowCreate: boolean;
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
  onChange,
  onSelect,
}: Props) {
  const [isOpen, setIsOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement>(null);

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
        className="nopan w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-slate-700 bg-slate-900 p-0 shadow-lg"
      >
        <SheetTabListPanel
          credentialId={credentialId}
          spreadsheetUrl={spreadsheetUrl}
          filter={value}
          allowCreate={allowCreate}
          onPick={(name) => {
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
  onPick: (tabName: string) => void;
};

function SheetTabListPanel({
  credentialId,
  spreadsheetUrl,
  filter,
  allowCreate,
  onPick,
}: ListPanelProps) {
  const tabsQuery = useGoogleSheetTabs({
    credentialId,
    spreadsheetUrlOrId: spreadsheetUrl,
    enabled: true,
  });
  const createMutation = useCreateGoogleSheetTab();

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
    const tab = await createMutation.mutateAsync({
      credentialId,
      spreadsheetUrlOrId: spreadsheetUrl,
      title,
    });
    onPick(tab.title);
  };

  return (
    <>
      {allowCreate ? (
        <div className="border-b border-slate-700">
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
          <div className="px-3 py-3 text-xs text-slate-500">No sheets.</div>
        ) : (
          tabs.map((tab) => (
            <button
              key={tab.sheet_id}
              type="button"
              onClick={() => onPick(tab.title)}
              className="flex w-full px-3 py-2 text-left text-xs text-slate-200 hover:bg-slate-700"
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
