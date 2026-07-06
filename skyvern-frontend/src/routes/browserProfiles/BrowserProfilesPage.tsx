import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";

import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { TableSearchInput } from "@/components/TableSearchInput";

import { BrowserProfilesList } from "./BrowserProfilesList";
import { CreateBrowserProfileButton } from "./CreateBrowserProfileButton";
import { useBackgroundBrowserProfileCreate } from "./hooks/useBackgroundBrowserProfileCreate";

const subHeaderText =
  "Saved browser state — cookies, logins, and settings — that agents launch a fresh browser from. Cheap to store and reusable in parallel, so one saved login can fan out across many concurrent runs.";

function BrowserProfilesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 250);
  const [profileType, setProfileType] = useState<"all" | "user" | "managed">(
    "all",
  );
  const managed = profileType === "all" ? undefined : profileType === "managed";

  function resetToFirstPage() {
    const params = new URLSearchParams(searchParams);
    params.set("page", "1");
    setSearchParams(params, { replace: true });
  }

  // Mounted for its side effects: rehydrates an in-progress create from
  // sessionStorage so the toast still fires if the user navigates here from
  // the session page mid-create or reloads the tab.
  useBackgroundBrowserProfileCreate();

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <BrowserIcon className="size-6" />
          <h1 className="text-2xl">Browser Profiles</h1>
        </div>
        <p className="text-sm leading-6 text-muted-foreground">
          {subHeaderText}
        </p>
      </div>
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <TableSearchInput
            value={search}
            onChange={(value) => {
              setSearch(value);
              resetToFirstPage();
            }}
            placeholder="Search browser profiles..."
            className="w-48 lg:w-72"
          />
          <select
            aria-label="Filter browser profiles by type"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={profileType}
            onChange={(e) => {
              setProfileType(e.target.value as "all" | "user" | "managed");
              resetToFirstPage();
            }}
          >
            <option value="all">All</option>
            <option value="user">User profiles</option>
            <option value="managed">Auto-managed</option>
          </select>
        </div>
        <CreateBrowserProfileButton />
      </div>
      <BrowserProfilesList searchKey={debouncedSearch} managed={managed} />
    </div>
  );
}

export { BrowserProfilesPage };
