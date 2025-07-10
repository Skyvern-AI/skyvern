import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  setWorkflowTemplatesRepo,
  setWorkflowTemplatesDir,
} from "./hooks/useLocalWorkflowTemplates";

interface RepoSettingsProps {
  currentRepo: string;
  currentDir: string;
  onSettingsChange: () => void;
}

export function RepoSettings({
  currentRepo,
  currentDir,
  onSettingsChange,
}: RepoSettingsProps) {
  const [repo, setRepo] = useState(currentRepo);
  const [dir, setDir] = useState(currentDir);
  const [isOpen, setIsOpen] = useState(false);

  const handleSave = () => {
    setWorkflowTemplatesRepo(repo);
    setWorkflowTemplatesDir(dir);
    onSettingsChange();
    setIsOpen(false);
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="shrink-0 text-xs text-slate-400 transition-colors hover:text-slate-300"
        title="Change repository and directory"
      >
        📁 {currentRepo}/{currentDir}
      </button>
    );
  }

  return (
    <div className="relative shrink-0">
      {/* Hidden placeholder to maintain layout */}
      <div className="invisible text-xs">
        📁 {currentRepo}/{currentDir}
      </div>

      {/* Actual form positioned absolutely */}
      <div className="absolute right-0 top-0 z-10 flex min-w-[300px] flex-col gap-2 rounded border bg-slate-800 p-3 shadow-lg">
        <div className="text-xs font-medium text-slate-300">
          Workflow Templates Source
        </div>

        <div>
          <label className="mb-1 block text-xs text-slate-400">
            Repository (owner/name)
          </label>
          <Input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="owner/repository"
            className="h-8 text-xs"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs text-slate-400">Directory</label>
          <Input
            value={dir}
            onChange={(e) => setDir(e.target.value)}
            placeholder="path/to/templates"
            className="h-8 text-xs"
          />
        </div>

        <div className="flex items-center gap-2">
          <Button onClick={handleSave} size="sm" className="h-8">
            Save
          </Button>
          <Button
            onClick={() => setIsOpen(false)}
            variant="outline"
            size="sm"
            className="h-8"
          >
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}
