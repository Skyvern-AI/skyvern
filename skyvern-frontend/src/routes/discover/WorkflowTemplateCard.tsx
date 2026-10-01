import { ArrowRightIcon, FileTextIcon } from "@radix-ui/react-icons";
import type { MouseEvent } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/util/utils";
import {
  TEMPLATE_CATEGORIES,
  splitTemplateTitle,
  type TemplateCategory,
} from "./templateCatalog";

type Props = {
  title: string;
  image?: string;
  category: TemplateCategory | null;
  popular?: boolean;
  to: string;
  onClick: (event?: MouseEvent<HTMLAnchorElement>) => void;
  className?: string;
};

function WorkflowTemplateCard({
  title,
  image,
  category,
  popular = false,
  to,
  onClick,
  className,
}: Props) {
  const { name, publisher } = splitTemplateTitle(title);
  const style = category ? TEMPLATE_CATEGORIES[category] : null;

  return (
    <Link
      to={to}
      onClick={(event) => onClick(event)}
      onAuxClick={(event) => {
        if (event.button === 1) onClick();
      }}
      className={cn(
        "group flex flex-col overflow-hidden rounded-2xl border border-border/70 bg-slate-elevation1 text-foreground transition-[border-color,box-shadow,transform] hover:-translate-y-0.5 hover:border-foreground/20 hover:shadow-[0_12px_32px_rgba(0,0,0,0.08)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 motion-reduce:transition-none motion-reduce:hover:translate-y-0 dark:hover:shadow-[0_12px_32px_rgba(0,0,0,0.35)]",
        className,
      )}
    >
      <div
        className={cn(
          "relative h-[150px] overflow-hidden md:h-44",
          style?.stage ?? "bg-slate-elevation2",
        )}
      >
        {popular ? (
          <span className="absolute left-3 top-3 z-10 flex h-6 items-center rounded-full bg-cta px-2.5 text-[11px] font-semibold tracking-wide text-cta-foreground">
            Most used
          </span>
        ) : null}
        {image ? (
          <img
            src={image}
            alt=""
            className="absolute left-1/2 top-9 w-[220px] -translate-x-1/2 -rotate-2 rounded-[10px] shadow-[0_14px_30px_rgba(2,8,23,0.35),0_0_0_1px_rgba(148,163,184,0.18)] transition-transform duration-300 group-hover:-translate-y-1 group-hover:rotate-0 motion-reduce:transition-none md:top-10 md:w-[270px]"
          />
        ) : (
          <FileTextIcon
            aria-hidden="true"
            className={cn(
              "absolute left-1/2 top-1/2 size-10 -translate-x-1/2 -translate-y-1/2",
              style?.ink ?? "text-muted-foreground",
            )}
          />
        )}
      </div>
      <div className="flex flex-1 flex-col gap-1.5 px-4 pb-4 pt-3.5">
        <span
          className={cn(
            "text-[11px] font-semibold uppercase leading-4 tracking-wider",
            style?.ink ?? "text-muted-foreground",
          )}
        >
          {style?.label ?? "Template"}
        </span>
        <span
          className="line-clamp-2 text-[15px] font-semibold leading-[21px]"
          title={title}
        >
          {name}
        </span>
        <span className="truncate text-[12.5px] leading-[17px] text-muted-foreground">
          {publisher ?? "Skyvern template"}
        </span>
        <span className="mt-auto flex items-center gap-1.5 pt-2 text-[13px] font-medium">
          Use template
          <ArrowRightIcon
            aria-hidden="true"
            className="size-3.5 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none"
          />
        </span>
      </div>
    </Link>
  );
}

export { WorkflowTemplateCard };
