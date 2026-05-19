import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";

import { TableSearchInput } from "@/components/TableSearchInput";

import { BrowserProfilesList } from "./BrowserProfilesList";
import { CreateBrowserProfileButton } from "./CreateBrowserProfileButton";
import { useBackgroundBrowserProfileCreate } from "./hooks/useBackgroundBrowserProfileCreate";

const subHeaderText =
  "Manage saved browser profiles used by your workflow runs.";

function BrowserProfilesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 250);

  // Mounted for its side effects: rehydrates an in-progress create from
  // sessionStorage so the toast still fires if the user navigates here from
  // the session page mid-create or reloads the tab.
  useBackgroundBrowserProfileCreate();

  return (
    <div className="space-y-5">
      <h1 className="text-2xl">Browser Profiles</h1>
      <div className="w-96 text-sm text-slate-300">{subHeaderText}</div>
      <div className="flex items-center justify-between gap-4">
        <TableSearchInput
          value={search}
          onChange={(value) => {
            setSearch(value);
            const params = new URLSearchParams(searchParams);
            params.set("page", "1");
            setSearchParams(params, { replace: true });
          }}
          placeholder="Search browser profiles..."
          className="w-48 lg:w-72"
        />
        <CreateBrowserProfileButton />
      </div>
      <BrowserProfilesList searchKey={debouncedSearch} />
    </div>
  );
}

export { BrowserProfilesPage };
