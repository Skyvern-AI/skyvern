import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { BookmarkFilledIcon, MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { useOrgTemplatesQuery } from "../hooks/useOrgTemplatesQuery";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { cn } from "@/util/utils";

interface CreateFromTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelectTemplate: (template: WorkflowApiResponse) => void;
}

function CreateFromTemplateDialog({
  open,
  onOpenChange,
  onSelectTemplate,
}: CreateFromTemplateDialogProps) {
  const [search, setSearch] = useState("");
  const { data: templates = [], isLoading } = useOrgTemplatesQuery();

  const filteredTemplates = templates.filter((template) =>
    template.title.toLowerCase().includes(search.toLowerCase()),
  );

  const handleSelect = (template: WorkflowApiResponse) => {
    onSelectTemplate(template);
    onOpenChange(false);
    setSearch("");
  };

  const handleOpenChange = (open: boolean) => {
    onOpenChange(open);
    if (!open) {
      setSearch("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Create from Template</DialogTitle>
          <DialogDescription>
            Select a template to create a new workflow with pre-filled blocks.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="relative">
            <MagnifyingGlassIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search templates..."
              className="pl-9"
              autoFocus
            />
          </div>
          <div className="max-h-[400px] overflow-y-auto">
            {isLoading ? (
              <div className="grid grid-cols-2 gap-3">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-24 rounded-lg" />
                ))}
              </div>
            ) : filteredTemplates.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <BookmarkFilledIcon className="mb-3 h-10 w-10 text-slate-400" />
                {templates.length === 0 ? (
                  <>
                    <p className="text-slate-600 dark:text-slate-300">
                      No templates yet
                    </p>
                    <p className="text-sm text-slate-400">
                      Save a workflow as a template to see it here.
                    </p>
                  </>
                ) : (
                  <>
                    <p className="text-slate-600 dark:text-slate-300">
                      No templates match your search
                    </p>
                    <p className="text-sm text-slate-400">
                      Try a different search term.
                    </p>
                  </>
                )}
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                {filteredTemplates.map((template) => (
                  <Button
                    key={template.workflow_permanent_id}
                    variant="outline"
                    className={cn(
                      "flex h-auto flex-col items-start gap-1 p-4 text-left",
                      "hover:border-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950/30",
                    )}
                    onClick={() => handleSelect(template)}
                  >
                    <div className="flex w-full items-center gap-2">
                      <BookmarkFilledIcon className="h-4 w-4 shrink-0 text-blue-500" />
                      <span className="truncate font-medium">
                        {template.title}
                      </span>
                    </div>
                    {template.description && (
                      <p className="line-clamp-2 text-xs text-slate-500">
                        {template.description}
                      </p>
                    )}
                    <p className="text-xs text-slate-400">
                      {template.workflow_definition.blocks.length} block
                      {template.workflow_definition.blocks.length !== 1
                        ? "s"
                        : ""}
                    </p>
                  </Button>
                ))}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export { CreateFromTemplateDialog };
