import { CheckIcon, ChevronDownIcon, CodeIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useDebounce } from "use-debounce";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse } from "@/api/types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useBrowserProfileQuery } from "@/routes/browserProfiles/hooks/useBrowserProfileQuery";
import { cn } from "@/util/utils";

import { useInfiniteBrowserProfilesQuery } from "../hooks/useInfiniteBrowserProfilesQuery";
import {
  classifyProfileRole,
  codeCaption,
  pickCaption,
  roleBadgeLabel,
  roleBadgeTooltip,
  roleSubtitle,
  type ProfileRole,
} from "./browserProfileControlModel";

type Props = {
  mode: "dropdown" | "code";
  profileId: string | null;
  onProfileChange: (profileId: string | null) => void;
  codeValue: string;
  onCodeChange: (value: string) => void;
  /** "none" = dropdown only (run form). "expression" = the per-input key picker. */
  codeMode: "none" | "expression";
  /** Never-blank Auto caption (consumer computes via autoCaption). */
  restingCaption: string;
  /** Resting label when nothing is picked; defaults to "Auto" (run form keeps it). */
  restingLabel?: string;
  /** Node id for the per-input Add-input picker. */
  nodeId?: string;
};

function BrowserProfileControl({
  mode,
  profileId,
  onProfileChange,
  codeValue,
  onCodeChange,
  codeMode,
  restingCaption,
  restingLabel = "Auto",
  nodeId,
}: Props) {
  if (mode === "code" && codeMode === "expression") {
    return (
      <div className="min-w-0 space-y-1.5">
        <WorkflowBlockInputTextarea
          nodeId={nodeId ?? ""}
          value={codeValue}
          onChange={(value) => onCodeChange(value)}
          placeholder="{{ credential_id }}"
          className="nopan text-xs"
        />
        <p className="whitespace-normal break-words text-xs text-muted-foreground">
          {codeCaption()}
        </p>
      </div>
    );
  }
  return (
    <ProfileDropdown
      profileId={profileId}
      onProfileChange={onProfileChange}
      restingCaption={restingCaption}
      restingLabel={restingLabel}
    />
  );
}

function ProfileDropdown({
  profileId,
  onProfileChange,
  restingCaption,
  restingLabel,
}: {
  profileId: string | null;
  onProfileChange: (profileId: string | null) => void;
  restingCaption: string;
  restingLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery] = useDebounce(query, 300);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  // F3: ＋New profile creates a blank named profile (omit browser_session_id) and
  // selects it — replaces the hidden own-memory; the first run fills it.
  const createProfile = useMutation({
    mutationFn: async (name: string) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const res = await client.post<BrowserProfileApiResponse>(
        "/browser_profiles",
        { name },
      );
      return res.data;
    },
    onSuccess: (profile) => {
      queryClient.invalidateQueries({ queryKey: ["browserProfiles-infinite"] });
      onProfileChange(profile.browser_profile_id);
      setOpen(false);
      setCreating(false);
      setNewName("");
      setQuery("");
    },
  });
  const { data, isFetching } = useInfiniteBrowserProfilesQuery({
    enabled: open,
    page_size: 100,
    searchKey: debouncedQuery.trim() || undefined,
  });
  const profiles = useMemo(
    () =>
      (data?.pages.flatMap((page) => page) ?? []).filter((p) => !p.deleted_at),
    [data],
  );

  // Resolve the picked profile by id so the closed trigger shows its real name
  // and role instead of a misleading "Auto" (the list only loads once opened).
  const { data: selectedById } = useBrowserProfileQuery(profileId ?? undefined);
  const selected =
    profiles.find((p) => p.browser_profile_id === profileId) ?? selectedById;
  const selectedRole: ProfileRole | null = selected
    ? classifyProfileRole(selected)
    : null;

  const triggerLabel = selected ? selected.name : restingLabel;
  const triggerSub = selected ? roleSubtitle(selected) : restingCaption;
  const caption = selected && selectedRole ? pickCaption(selectedRole) : "";

  const pick = (id: string | null) => {
    onProfileChange(id);
    setOpen(false);
    setQuery("");
  };

  const searching = debouncedQuery.trim().length > 0;
  // ＋New sits under the resting option in the default view; under an active
  // search the resting option hides, so keep ＋New at the bottom to leave the
  // first matching profile as cmdk's default-highlighted (Enter) target.
  const newProfileGroup = (borderClass: string) => (
    <CommandGroup className={borderClass}>
      <CommandItem value="new-profile" onSelect={() => setCreating(true)}>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">＋ New profile…</div>
          <div className="whitespace-normal break-words text-xs text-muted-foreground">
            Created empty — the first run fills it, every run updates it
          </div>
        </div>
      </CommandItem>
    </CommandGroup>
  );

  return (
    <div className="min-w-0 space-y-1.5">
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) {
            setQuery("");
            setCreating(false);
            setNewName("");
          }
        }}
      >
        <PopoverTrigger asChild>
          <button
            type="button"
            role="combobox"
            aria-expanded={open}
            className="nopan flex w-full min-w-0 items-center justify-between rounded-md border border-input bg-transparent px-3 py-2 text-left text-sm shadow-sm"
          >
            <span className="flex min-w-0 flex-col">
              <span className="flex min-w-0 items-center gap-1.5">
                <span className="truncate text-foreground dark:text-slate-200">
                  {triggerLabel}
                </span>
                {selectedRole ? (
                  <span className="shrink-0 rounded bg-slate-elevation3 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {roleBadgeLabel(selectedRole)}
                  </span>
                ) : null}
              </span>
              <span className="whitespace-normal break-words text-xs text-muted-foreground">
                {triggerSub}
              </span>
            </span>
            <ChevronDownIcon className="ml-2 size-4 shrink-0 self-start text-muted-foreground opacity-50" />
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-[var(--radix-popover-trigger-width)] p-0"
        >
          {creating ? (
            <div className="space-y-2 p-2">
              <Input
                autoFocus
                placeholder="Profile name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    newName.trim() &&
                    !createProfile.isPending
                  ) {
                    createProfile.mutate(newName.trim());
                  }
                }}
              />
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setCreating(false);
                    setNewName("");
                  }}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={!newName.trim() || createProfile.isPending}
                  onClick={() => createProfile.mutate(newName.trim())}
                >
                  Create
                </Button>
              </div>
            </div>
          ) : (
            <Command shouldFilter={false}>
              <CommandInput
                placeholder="Search profiles..."
                value={query}
                onValueChange={setQuery}
              />
              <CommandList>
                {!searching ? (
                  <>
                    <CommandGroup>
                      <CommandItem
                        value="auto-profile"
                        onSelect={() => pick(null)}
                      >
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium">
                            {restingLabel}
                          </div>
                          <div className="whitespace-normal break-words text-xs text-muted-foreground">
                            {restingCaption}
                          </div>
                        </div>
                        {profileId === null ? (
                          <CheckIcon className="ml-2 size-4 shrink-0" />
                        ) : null}
                      </CommandItem>
                    </CommandGroup>
                    {newProfileGroup("border-b border-border")}
                  </>
                ) : null}
                {profiles.length === 0 ? (
                  <CommandEmpty>
                    {isFetching
                      ? "Searching profiles…"
                      : debouncedQuery.trim()
                        ? `No profiles match "${debouncedQuery.trim()}"`
                        : "No saved profiles yet."}
                  </CommandEmpty>
                ) : (
                  <CommandGroup>
                    {profiles.map((p) => {
                      const role = classifyProfileRole(p);
                      return (
                        <CommandItem
                          key={p.browser_profile_id}
                          value={`profile-${p.browser_profile_id}`}
                          onSelect={() => pick(p.browser_profile_id)}
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate text-sm font-medium">
                                {p.name}
                              </span>
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <span
                                      tabIndex={0}
                                      className="shrink-0 rounded bg-slate-elevation3 px-1.5 py-0.5 text-[10px] text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                    >
                                      {roleBadgeLabel(role)}
                                    </span>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    {roleBadgeTooltip(role)}
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            </div>
                            <div className="whitespace-normal break-words text-xs text-muted-foreground">
                              {roleSubtitle(p)}
                            </div>
                          </div>
                          {profileId === p.browser_profile_id ? (
                            <CheckIcon className="ml-2 size-4 shrink-0" />
                          ) : null}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
                {searching ? newProfileGroup("border-t border-border") : null}
              </CommandList>
            </Command>
          )}
        </PopoverContent>
      </Popover>
      {caption ? (
        <p className="whitespace-normal break-words text-xs text-muted-foreground">
          {caption}
        </p>
      ) : null}
    </div>
  );
}

// Compact code-mode toggle (the Bitwarden/1Password-style </> affordance) in the
// header-row right slot: off = one saved profile, on = an expression resolving
// one saved profile per rendered value of an input key.
function ProfileModeToggle({
  active,
  disabled = false,
  disabledTooltip,
  onToggle,
}: {
  active: boolean;
  disabled?: boolean;
  disabledTooltip?: string;
  onToggle: () => void;
}) {
  const button = (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={active}
      aria-label="Use an expression — one profile per input value"
      onClick={onToggle}
      className={cn(
        "nopan inline-flex size-6 shrink-0 items-center justify-center rounded-md border border-input transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        active
          ? "bg-muted text-foreground dark:bg-slate-700"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      <CodeIcon className="size-3.5" />
    </button>
  );
  const tooltip = disabled
    ? disabledTooltip
    : "Switch to an expression — one profile per input value";
  if (!tooltip) return button;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          {disabled ? (
            <span
              tabIndex={0}
              className="inline-flex rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {button}
            </span>
          ) : (
            button
          )}
        </TooltipTrigger>
        <TooltipContent>{tooltip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { BrowserProfileControl, ProfileModeToggle };
