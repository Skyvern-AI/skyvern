import { CheckIcon, Link2Icon } from "@radix-ui/react-icons";

import type { ConnectedAccountChoice } from "../workflowCopilotTypes";
import { connectedAccountChoiceLabel } from "./connectedAccountChoiceLabel";

type ConnectedAccountChoiceCardProps = {
  choices: ConnectedAccountChoice[];
  selectedConnectionId: string | null;
  disabled: boolean;
  onSelect: (connectionId: string) => void;
};

export function ConnectedAccountChoiceCard({
  choices,
  selectedConnectionId,
  disabled,
  onSelect,
}: ConnectedAccountChoiceCardProps) {
  return (
    <div
      className="overflow-hidden rounded-lg border border-border bg-slate-elevation2"
      role="group"
      aria-label="Connected Google accounts"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="flex size-6 flex-none items-center justify-center rounded-md bg-background">
          <Link2Icon className="size-4 text-muted-foreground" />
        </span>
        <span className="text-xs font-semibold text-foreground">
          Choose a Google account
        </span>
      </div>
      <div className="p-1">
        {choices.map((choice) => {
          const selected = selectedConnectionId === choice.connection_id;
          const accountLabel = connectedAccountChoiceLabel(choice, choices);
          const content = (
            <>
              <Link2Icon className="size-4 flex-none text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-foreground">
                  {choice.name}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground">
                  {accountLabel}
                </span>
              </span>
              <span className="flex flex-none items-center gap-1.5 text-[11px] capitalize text-muted-foreground">
                <span
                  className={`size-1.5 rounded-full ${choice.state === "active" ? "bg-success" : "bg-warning"}`}
                  aria-hidden="true"
                />
                {choice.state === "active" ? "active" : "Reconnect"}
              </span>
              {selected ? (
                <span className="flex-none text-success">
                  <CheckIcon className="size-4" aria-hidden="true" />
                  <span className="sr-only">Selected</span>
                </span>
              ) : null}
            </>
          );
          if (choice.state !== "active") {
            return (
              <a
                key={choice.connection_id}
                href="/integrations"
                target="_blank"
                rel="noreferrer"
                className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left outline-none transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              >
                {content}
              </a>
            );
          }
          return (
            <button
              key={choice.connection_id}
              type="button"
              disabled={disabled || selected}
              aria-pressed={selected}
              onClick={() => onSelect(choice.connection_id)}
              className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left outline-none transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-default disabled:opacity-60"
            >
              {content}
            </button>
          );
        })}
      </div>
    </div>
  );
}
