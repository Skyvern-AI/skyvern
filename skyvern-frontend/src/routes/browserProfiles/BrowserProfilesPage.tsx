import { BrowserProfilesList } from "./BrowserProfilesList";

const subHeaderText =
  "Manage saved browser profiles used by your workflow runs.";

function BrowserProfilesPage() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl">Browser Profiles</h1>
      <div className="w-96 text-sm text-slate-300">{subHeaderText}</div>
      <BrowserProfilesList />
    </div>
  );
}

export { BrowserProfilesPage };
