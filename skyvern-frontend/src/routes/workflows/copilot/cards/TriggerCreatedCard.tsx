import type { ComponentType } from "react";
import { LightningBoltIcon, ReloadIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";

import type { CreatedTrigger } from "../narrativeState";
import {
  CardBody,
  CardFooter,
  CardHeader,
  CardPill,
  CopilotCard,
} from "./cardChrome";
import { ACCEPT_BUTTON_CLASS } from "./cardLayout";

export type TriggerSourceDisplay = Readonly<{
  name: string;
  Logo: ComponentType<{ className?: string }>;
}>;

const FALLBACK_SOURCE: TriggerSourceDisplay = {
  name: "Event",
  Logo: LightningBoltIcon,
};

export function TriggerCreatedCard({
  trigger,
  onOpen,
  enabled,
  onTurnOn,
  turningOn = false,
  source = FALLBACK_SOURCE,
}: {
  trigger: CreatedTrigger;
  // The source's name and logo, supplied by the deployment that knows its sources.
  source?: TriggerSourceDisplay;
  // Absent outside the Studio, where there is no Triggers panel to open.
  onOpen?: () => void;
  // The trigger's current state; undefined while it is unknown (loading, deleted, or not visible to this user).
  enabled?: boolean;
  // Absent when the user cannot switch triggers or the source needs more setup in the panel first.
  onTurnOn?: () => void;
  turningOn?: boolean;
}) {
  const { name, Logo } = source;
  return (
    <CopilotCard>
      <CardHeader
        icon={<Logo className="size-4 text-muted-foreground" />}
        title={`${name} trigger created`}
        wrapTitle
        right={
          enabled === undefined ? null : enabled ? (
            <CardPill tone="green">On</CardPill>
          ) : (
            <CardPill tone="amber">Off</CardPill>
          )
        }
      />
      {trigger.filters.length > 0 ? (
        <CardBody>
          {/* Indented to the title: the 18px icon column plus the header's 10px gap. */}
          <dl
            aria-label="Filters"
            className="ml-7 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-xs leading-snug"
          >
            {trigger.filters.map(({ label, value }) => (
              <div key={label} className="contents">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-words font-medium text-foreground dark:text-slate-200">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </CardBody>
      ) : null}
      <CardFooter className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <span
          role="status"
          className="text-[11px] leading-snug text-muted-foreground"
        >
          {enabled === undefined
            ? "See whether it is on in Triggers."
            : enabled
              ? trigger.firesOnItsOwn
                ? "On. Each new match starts one run."
                : "On. Senders need its signing secret from Triggers."
              : "It starts no runs until you turn it on."}
        </span>
        <span className="flex items-center gap-2">
          {onOpen ? (
            <Button size="sm" variant="secondary" onClick={onOpen}>
              Open in Triggers
            </Button>
          ) : null}
          {onTurnOn && enabled === false ? (
            <Button
              size="sm"
              className={`${ACCEPT_BUTTON_CLASS} relative`}
              disabled={turningOn}
              aria-busy={turningOn}
              onClick={onTurnOn}
            >
              {/* The spinner sits over the label so the button keeps its width and the footer does not reflow. */}
              <span className={turningOn ? "invisible" : undefined}>
                Turn on
              </span>
              {turningOn ? (
                <ReloadIcon
                  className="absolute size-3.5 animate-spin"
                  aria-hidden
                />
              ) : null}
            </Button>
          ) : null}
        </span>
      </CardFooter>
    </CopilotCard>
  );
}
