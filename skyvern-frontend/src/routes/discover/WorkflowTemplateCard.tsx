import { LightningBoltIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";

type Props = {
  title: string;
  onClick: () => void;
};

function WorkflowTemplateCard({ title, onClick }: Props) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "group flex h-52 w-full cursor-pointer flex-col overflow-hidden rounded-xl border border-border bg-card text-left shadow-card transition-shadow hover:shadow-card-hover",
      )}
    >
      <div className="relative flex h-28 items-center justify-center bg-gradient-to-br from-brand-soft to-slate-elevation2">
        <div className="flex size-12 items-center justify-center rounded-full bg-brand-cta text-brand-cta-foreground shadow-sm">
          <LightningBoltIcon className="size-6" />
        </div>
      </div>
      <div className="flex h-24 flex-col gap-1 p-3">
        <h1
          className="line-clamp-2 overflow-hidden text-ellipsis text-sm font-medium text-foreground"
          title={title}
        >
          {title}
        </h1>
        <p className="text-xs text-muted-foreground">Template</p>
      </div>
    </button>
  );
}

export { WorkflowTemplateCard };
