import { useId, useState } from "react";
import { Cross2Icon, PlusIcon } from "@radix-ui/react-icons";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type {
  WorkflowRetryPolicy,
  WorkflowRetryStatus,
  WorkflowRetryWebhookMode,
} from "@/routes/workflows/types/workflowTypes";
import { ErrorCodeChipInput } from "./ErrorCodeChipInput";
import { clampRetryInteger, normalizeRetryPolicy } from "./retryPolicyUtils";

const statuses = [
  { value: "failed", label: "Failed" },
  { value: "terminated", label: "Terminated" },
  { value: "timed_out", label: "Timed out" },
  { value: "completed", label: "Completed" },
] as const;

type Props = {
  value: WorkflowRetryPolicy | null;
  onChange: (policy: WorkflowRetryPolicy | null) => void;
  knownErrorCodes: Array<string>;
  readOnly: boolean;
};

export function RetryPolicyEditor({
  value,
  onChange,
  knownErrorCodes,
  readOnly,
}: Props) {
  const id = useId();
  const [maxRetriesDraft, setMaxRetriesDraft] = useState<string | null>(null);
  const [delayDraft, setDelayDraft] = useState<string | null>(null);
  const unusedStatuses = statuses.filter(
    (status) => !value?.retry_on.some((rule) => rule.status === status.value),
  );
  function update(policy: WorkflowRetryPolicy | null) {
    if (!readOnly) onChange(normalizeRetryPolicy(policy));
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Label htmlFor={`${id}-enabled`}>Automatic retry</Label>
        <HelpTooltip content="Retries restart the workflow from the beginning. All attempts share one workflow run ID. Each attempt uses credits." />
        <Switch
          id={`${id}-enabled`}
          className="ml-auto"
          disabled={readOnly}
          checked={value !== null}
          onCheckedChange={(enabled) => {
            setMaxRetriesDraft(null);
            setDelayDraft(null);
            update(
              enabled
                ? {
                    max_retries: 1,
                    delay_seconds: 0,
                    webhook_on_retry: "final_only",
                    retry_on: [{ status: "failed" }],
                  }
                : null,
            );
          }}
        />
      </div>
      {value && (
        <div className="flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4">
          {value.retry_on.map((rule, index) => (
            <div
              key={index}
              className="space-y-2 rounded-md bg-slate-elevation5 p-3"
            >
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1 space-y-2">
                  <Label htmlFor={`${id}-status-${index}`}>Status</Label>
                  <Select
                    value={rule.status}
                    disabled={readOnly}
                    onValueChange={(status) =>
                      update({
                        ...value,
                        retry_on: value.retry_on.map((item, i) =>
                          i === index
                            ? { ...item, status: status as WorkflowRetryStatus }
                            : item,
                        ),
                      })
                    }
                  >
                    <SelectTrigger id={`${id}-status-${index}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {rule.status === "canceled" && (
                        <SelectItem value="canceled">
                          Canceled (never retried)
                        </SelectItem>
                      )}
                      {statuses
                        .filter(
                          (status) =>
                            status.value === rule.status ||
                            unusedStatuses.includes(status),
                        )
                        .map((status) => (
                          <SelectItem key={status.value} value={status.value}>
                            {status.label}
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove ${rule.status} condition`}
                  disabled={readOnly || value.retry_on.length === 1}
                  onClick={() =>
                    update({
                      ...value,
                      retry_on: value.retry_on.filter((_, i) => i !== index),
                    })
                  }
                >
                  <Cross2Icon className="size-4" />
                </Button>
              </div>
              <ErrorCodeChipInput
                value={rule.error_codes ?? []}
                knownErrorCodes={knownErrorCodes}
                readOnly={readOnly}
                onChange={(codes) =>
                  update({
                    ...value,
                    retry_on: value.retry_on.map((item, i) =>
                      i === index ? { ...item, error_codes: codes } : item,
                    ),
                  })
                }
              />
            </div>
          ))}
          <Button
            type="button"
            variant="secondary"
            disabled={readOnly || unusedStatuses.length === 0}
            onClick={() => {
              const status = unusedStatuses[0]?.value;
              if (status)
                update({ ...value, retry_on: [...value.retry_on, { status }] });
            }}
          >
            <PlusIcon className="mr-2 size-4" />
            Add condition
          </Button>
          <div className="space-y-2">
            <Label htmlFor={`${id}-max`}>Maximum retries</Label>
            <Input
              id={`${id}-max`}
              type="number"
              min={1}
              max={5}
              step={1}
              disabled={readOnly}
              value={maxRetriesDraft ?? value.max_retries}
              onChange={(event) => {
                const text = event.target.value;
                setMaxRetriesDraft(text);
                if (text.trim() !== "" && Number.isFinite(Number(text))) {
                  update({
                    ...value,
                    max_retries: clampRetryInteger(text, 1, 5, 1),
                  });
                }
              }}
              onBlur={(event) => {
                setMaxRetriesDraft(null);
                update({
                  ...value,
                  max_retries: clampRetryInteger(event.target.value, 1, 5, 1),
                });
              }}
            />
            <p className="text-xs text-muted-foreground">
              Total attempts = retries + 1
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${id}-delay`}>
              Delay between attempts (seconds)
            </Label>
            <Input
              id={`${id}-delay`}
              type="number"
              min={0}
              max={3600}
              step={1}
              disabled={readOnly}
              value={delayDraft ?? value.delay_seconds}
              onChange={(event) => {
                const text = event.target.value;
                setDelayDraft(text);
                if (text.trim() !== "" && Number.isFinite(Number(text))) {
                  update({
                    ...value,
                    delay_seconds: clampRetryInteger(text, 0, 3600, 0),
                  });
                }
              }}
              onBlur={(event) => {
                setDelayDraft(null);
                update({
                  ...value,
                  delay_seconds: clampRetryInteger(
                    event.target.value,
                    0,
                    3600,
                    0,
                  ),
                });
              }}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${id}-webhook`}>Retry webhook</Label>
            <Select
              value={value.webhook_on_retry}
              disabled={readOnly}
              onValueChange={(mode) =>
                update({
                  ...value,
                  webhook_on_retry: mode as WorkflowRetryWebhookMode,
                })
              }
            >
              <SelectTrigger id={`${id}-webhook`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="final_only">Final attempt only</SelectItem>
                <SelectItem value="every_attempt">Every attempt</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      )}
    </div>
  );
}
