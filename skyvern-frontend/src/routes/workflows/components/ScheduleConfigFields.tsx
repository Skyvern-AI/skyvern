import { useEffect, useMemo, useState } from "react";
import {
  CaretSortIcon,
  CheckIcon,
  ChevronRightIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/util/utils";
import {
  cronToHumanReadable,
  formatNextRun,
  getNextRuns,
  getTimezones,
  isValidCron,
  meetsMinCronInterval,
} from "@/routes/workflows/editor/panels/schedulePanel/cronUtils";
import {
  DAY_OF_WEEK_OPTIONS,
  DEFAULT_SCHEDULE_BUILDER,
  cronToScheduleBuilder,
  scheduleBuilderToCron,
  scheduleBuildersEquivalent,
  to12Hour,
  to24Hour,
  type ScheduleBuilderState,
  type ScheduleFrequency,
} from "@/routes/workflows/editor/panels/schedulePanel/scheduleBuilder";

type Props = {
  cronExpression: string;
  timezone: string;
  onCronChange: (cronExpression: string) => void;
  onTimezoneChange: (timezone: string) => void;
  size?: "default" | "compact";
  disabled?: boolean;
};

const FREQUENCY_OPTIONS: ReadonlyArray<{
  value: ScheduleFrequency;
  label: string;
}> = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

const HOUR_OPTIONS = Array.from({ length: 12 }, (_, i) => i + 1);
const MINUTE_OPTIONS = Array.from({ length: 60 }, (_, i) => i);
const DAY_OF_MONTH_OPTIONS = Array.from({ length: 31 }, (_, i) => i + 1);

function ScheduleConfigFields({
  cronExpression,
  timezone,
  onCronChange,
  onTimezoneChange,
  size = "default",
  disabled = false,
}: Readonly<Props>) {
  const compact = size === "compact";
  const controlClass = compact ? "h-8 text-xs" : "h-9 text-sm";
  const labelClass = compact ? "text-xs" : undefined;

  const parsed = useMemo(
    () => cronToScheduleBuilder(cronExpression),
    [cronExpression],
  );
  const isCustom = parsed === null;

  const [builder, setBuilder] = useState<ScheduleBuilderState>(
    () => parsed ?? DEFAULT_SCHEDULE_BUILDER,
  );
  const [advancedOpen, setAdvancedOpen] = useState(isCustom);
  const [timezoneOpen, setTimezoneOpen] = useState(false);

  // Keep the pickers in step when the cron changes externally (raw edit,
  // dialog reset, seeding an existing schedule). Compares only the fields the
  // frequency actually uses so a generated cron round-trip doesn't clobber
  // unshown selections or spin a render loop.
  useEffect(() => {
    if (parsed && !scheduleBuildersEquivalent(parsed, builder)) {
      setBuilder(parsed);
    }
  }, [parsed, builder]);

  useEffect(() => {
    if (isCustom) setAdvancedOpen(true);
  }, [isCustom]);

  const allTimezones = useMemo(() => getTimezones(), []);
  const valid = isValidCron(cronExpression);
  const intervalTooShort = valid && !meetsMinCronInterval(cronExpression);
  const humanReadable = valid ? cronToHumanReadable(cronExpression) : null;
  const nextRuns = valid ? getNextRuns(cronExpression, timezone, 5) : [];

  const { hour12, meridiem } = to12Hour(builder.hour);

  function commitBuilder(next: ScheduleBuilderState) {
    setBuilder(next);
    onCronChange(scheduleBuilderToCron(next));
  }

  function handleFrequencyChange(value: string) {
    if (value === "custom") return;
    const frequency = value as ScheduleFrequency;
    const next: ScheduleBuilderState = { ...builder, frequency };
    if (frequency === "weekly" && next.daysOfWeek.length === 0) {
      next.daysOfWeek = [...DEFAULT_SCHEDULE_BUILDER.daysOfWeek];
    }
    commitBuilder(next);
  }

  function toggleDay(day: number) {
    const has = builder.daysOfWeek.includes(day);
    const days = has
      ? builder.daysOfWeek.filter((d) => d !== day)
      : [...builder.daysOfWeek, day];
    // A weekly schedule needs at least one day; ignore removing the last one.
    commitBuilder({ ...builder, daysOfWeek: days.length > 0 ? days : [day] });
  }

  const showTimeOfDay = builder.frequency !== "hourly";

  return (
    <div className={cn(compact ? "space-y-4" : "space-y-6")}>
      <div className="space-y-2">
        <Label className={labelClass}>Repeat</Label>
        <Select
          value={isCustom ? "custom" : builder.frequency}
          onValueChange={handleFrequencyChange}
          disabled={disabled}
        >
          <SelectTrigger className={cn("w-full", controlClass)}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FREQUENCY_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
            {isCustom && (
              <SelectItem value="custom" disabled>
                Custom
              </SelectItem>
            )}
          </SelectContent>
        </Select>
      </div>

      {!isCustom && builder.frequency === "hourly" && (
        <div className="space-y-2">
          <Label className={labelClass}>Minute past the hour</Label>
          <Select
            value={String(builder.minute)}
            onValueChange={(value) =>
              commitBuilder({ ...builder, minute: Number(value) })
            }
            disabled={disabled}
          >
            <SelectTrigger className={cn("w-24", controlClass)}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MINUTE_OPTIONS.map((minute) => (
                <SelectItem key={minute} value={String(minute)}>
                  :{String(minute).padStart(2, "0")}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      {!isCustom && builder.frequency === "weekly" && (
        <div className="space-y-2">
          <Label className={labelClass}>On these days</Label>
          <div className="flex flex-wrap gap-1.5">
            {DAY_OF_WEEK_OPTIONS.map((day) => {
              const selected = builder.daysOfWeek.includes(day.value);
              return (
                <Button
                  key={day.value}
                  type="button"
                  variant={selected ? "default" : "secondary"}
                  size="sm"
                  aria-pressed={selected}
                  aria-label={day.label}
                  className={cn("w-9 px-0", compact && "h-7")}
                  disabled={disabled}
                  onClick={() => toggleDay(day.value)}
                >
                  {day.short}
                </Button>
              );
            })}
          </div>
        </div>
      )}

      {!isCustom && builder.frequency === "monthly" && (
        <div className="space-y-2">
          <Label className={labelClass}>On day of month</Label>
          <Select
            value={String(builder.dayOfMonth)}
            onValueChange={(value) =>
              commitBuilder({ ...builder, dayOfMonth: Number(value) })
            }
            disabled={disabled}
          >
            <SelectTrigger className={cn("w-24", controlClass)}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DAY_OF_MONTH_OPTIONS.map((day) => (
                <SelectItem key={day} value={String(day)}>
                  {day}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      {!isCustom && showTimeOfDay && (
        <div className="space-y-2">
          <Label className={labelClass}>At</Label>
          <div className="flex items-center gap-2">
            <Select
              value={String(hour12)}
              onValueChange={(value) =>
                commitBuilder({
                  ...builder,
                  hour: to24Hour(Number(value), meridiem),
                })
              }
              disabled={disabled}
            >
              <SelectTrigger className={cn("w-20", controlClass)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {HOUR_OPTIONS.map((hour) => (
                  <SelectItem key={hour} value={String(hour)}>
                    {hour}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span className="text-muted-foreground">:</span>
            <Select
              value={String(builder.minute)}
              onValueChange={(value) =>
                commitBuilder({ ...builder, minute: Number(value) })
              }
              disabled={disabled}
            >
              <SelectTrigger className={cn("w-20", controlClass)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MINUTE_OPTIONS.map((minute) => (
                  <SelectItem key={minute} value={String(minute)}>
                    {String(minute).padStart(2, "0")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={meridiem}
              onValueChange={(value) =>
                commitBuilder({
                  ...builder,
                  hour: to24Hour(hour12, value as "AM" | "PM"),
                })
              }
              disabled={disabled}
            >
              <SelectTrigger className={cn("w-20", controlClass)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="AM">AM</SelectItem>
                <SelectItem value="PM">PM</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      )}

      <div className="space-y-2">
        <Label className={labelClass}>Timezone</Label>
        <Popover open={timezoneOpen} onOpenChange={setTimezoneOpen}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              role="combobox"
              aria-expanded={timezoneOpen}
              disabled={disabled}
              className={cn("w-full justify-between font-normal", controlClass)}
            >
              <span className="truncate">{timezone}</span>
              <CaretSortIcon className="ml-2 size-4 shrink-0 opacity-50" />
            </Button>
          </PopoverTrigger>
          <PopoverContent
            className="w-[var(--radix-popover-trigger-width)] p-0"
            align="start"
          >
            <Command>
              <CommandInput placeholder="Search timezone..." />
              <CommandList>
                <CommandEmpty>No timezone found.</CommandEmpty>
                <CommandGroup>
                  {allTimezones.map((tz) => (
                    <CommandItem
                      key={tz}
                      value={tz}
                      onSelect={() => {
                        onTimezoneChange(tz);
                        setTimezoneOpen(false);
                      }}
                    >
                      <CheckIcon
                        className={cn(
                          "mr-2 size-4",
                          tz === timezone ? "opacity-100" : "opacity-0",
                        )}
                      />
                      {tz}
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      </div>

      {humanReadable && (
        <p
          className={cn(
            "text-muted-foreground",
            compact ? "text-xs" : "text-sm",
          )}
        >
          {humanReadable}
        </p>
      )}

      <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="group flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
            Advanced (cron expression)
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-1.5 pt-2">
          <Input
            value={cronExpression}
            onChange={(e) => onCronChange(e.target.value)}
            placeholder="* * * * *"
            disabled={disabled}
            className={cn(
              controlClass,
              cronExpression &&
                (!valid || intervalTooShort) &&
                "border-destructive",
            )}
          />
          {!valid && cronExpression && (
            <p className="text-xs text-destructive">Invalid cron expression</p>
          )}
          {intervalTooShort && (
            <p className="text-xs text-destructive">
              Schedule runs must be at least 5 minutes apart.
            </p>
          )}
        </CollapsibleContent>
      </Collapsible>

      {nextRuns.length > 0 && (
        <div className="space-y-2">
          <Label className={labelClass}>Next Scheduled Runs</Label>
          <div className="space-y-1 rounded-md border border-border bg-slate-elevation3 p-3">
            {nextRuns.map((run) => (
              <div
                key={run.toISOString()}
                className="text-xs text-muted-foreground"
              >
                {formatNextRun(run, timezone)}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export { ScheduleConfigFields };
