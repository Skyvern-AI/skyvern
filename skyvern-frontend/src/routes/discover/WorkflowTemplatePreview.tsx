import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { WorkflowTemplate } from "./hooks/useLocalWorkflowTemplates";

interface WorkflowTemplatePreviewProps {
  template: WorkflowTemplate | null;
  isOpen: boolean;
  onClose: () => void;
  onSave?: () => void;
}

export function WorkflowTemplatePreview({
  template,
  isOpen,
  onClose,
  onSave,
}: WorkflowTemplatePreviewProps) {
  if (!template) return null;

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="flex max-h-[80vh] max-w-4xl flex-col">
        <DialogHeader>
          <DialogTitle>{template.title}</DialogTitle>
          <DialogDescription>{template.description}</DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1">
          <div className="h-full rounded-md border">
            <div className="border-b bg-slate-100 px-3 py-2 dark:bg-slate-800">
              <span className="font-mono text-xs text-slate-600 dark:text-slate-400">
                {template.name}.yaml
              </span>
            </div>
            <div className="h-full overflow-auto p-4">
              <pre className="whitespace-pre-wrap font-mono text-sm text-slate-900 dark:text-slate-100">
                {template.content}
              </pre>
            </div>
          </div>
        </div>

        <DialogFooter className="flex gap-2">
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          {onSave && (
            <Button onClick={onSave} className="bg-blue-600 hover:bg-blue-700">
              Save to Workflows
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
