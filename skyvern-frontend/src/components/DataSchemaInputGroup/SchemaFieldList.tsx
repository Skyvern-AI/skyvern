import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";

import type { SchemaField, SchemaOutline, SchemaView } from "./describeSchema";

function Chip({ children }: { children: string }) {
  return (
    <span
      title="Shown in JSON view"
      className="rounded border border-amber-700/40 px-1 font-mono text-[10px] text-amber-700 dark:border-amber-400/40 dark:text-amber-400"
    >
      {children}
      <span className="sr-only">, shown in JSON view</span>
    </span>
  );
}

function FieldRow({ field }: { field: SchemaField }) {
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-foreground dark:text-slate-200">
          {field.name}
        </span>
        {field.type !== "" && (
          <span className="rounded bg-slate-elevation3 px-1 font-mono text-[10px] text-tertiary-foreground">
            {field.type}
          </span>
        )}
        {field.nullable && (
          <span className="text-[10px] italic text-muted-foreground">
            or null
          </span>
        )}
        {field.optional && (
          <span className="text-[10px] italic text-muted-foreground">
            optional
          </span>
        )}
        {field.constraints.length > 0 && (
          <span className="text-[10px] text-muted-foreground">
            {field.constraints.join(" · ")}
          </span>
        )}
        {field.unsupported.map((keyword, index) => (
          <Chip key={index}>{keyword}</Chip>
        ))}
      </div>
      {field.description !== null && (
        <div className="whitespace-pre-wrap break-words text-muted-foreground">
          {field.description}
        </div>
      )}
      {field.children.length > 0 && <FieldRows fields={field.children} />}
    </li>
  );
}

function FieldRows({
  fields,
  className,
}: {
  fields: Array<SchemaField>;
  className?: string;
}) {
  return (
    <ul className={cn("space-y-2 border-l border-border pl-3", className)}>
      {fields.map((field, index) => (
        <FieldRow key={`${field.name}-${index}`} field={field} />
      ))}
    </ul>
  );
}

function hasChip(fields: Array<SchemaField>): boolean {
  return fields.some(
    (field) => field.unsupported.length > 0 || hasChip(field.children),
  );
}

function SchemaFieldList({ outline }: { outline: SchemaOutline }) {
  const hasHeader =
    outline.isList ||
    outline.description !== null ||
    outline.constraints.length > 0 ||
    outline.unsupported.length > 0;
  const showsChip = outline.unsupported.length > 0 || hasChip(outline.fields);
  return (
    <div
      data-testid="schema-field-list"
      role="region"
      aria-label="Data schema fields"
      tabIndex={0}
      className="nopan nowheel max-h-80 space-y-2 overflow-auto rounded-md bg-slate-elevation1 p-2 text-xs"
    >
      {hasHeader && (
        <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
          {outline.isList && <span>List of:</span>}
          {outline.description !== null && (
            <span className="whitespace-pre-wrap break-words">
              {outline.description}
            </span>
          )}
          {outline.constraints.length > 0 && (
            <span className="text-[10px]">
              {outline.constraints.join(" · ")}
            </span>
          )}
          {outline.unsupported.map((keyword, index) => (
            <Chip key={index}>{keyword}</Chip>
          ))}
        </div>
      )}
      {showsChip && (
        <div className="text-[10px] text-amber-700 dark:text-amber-400">
          Some parts of this schema show only in JSON view
        </div>
      )}
      <FieldRows fields={outline.fields} className="border-l-0 pl-0" />
    </div>
  );
}

function SchemaViewButton({
  view,
  onClick,
}: {
  view: SchemaView;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      variant="tertiary"
      aria-label={view === "fields" ? "Show JSON" : "Show fields"}
      onClick={onClick}
      className="nodrag nopan h-7 shrink-0 text-xs"
    >
      {view === "fields" ? "JSON" : "Fields"}
    </Button>
  );
}

export { SchemaFieldList, SchemaViewButton };
