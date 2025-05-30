import { useState } from "react";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ClockIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowApiResponse } from "../routes/workflows/types/workflowTypes";

interface CronSchedulerProps {
  workflow: WorkflowApiResponse;
}

const TIMEZONES = [
  "UTC",
  "America/Los_Angeles",
  "America/New_York",
  "Europe/London",
  "Europe/Paris",
  "Asia/Tokyo",
  "Asia/Shanghai",
  "Australia/Sydney",
];

const CRON_EXAMPLES = [
  { label: "Every hour", value: "0 * * * *" },
  { label: "Every day at midnight", value: "0 0 * * *" },
  { label: "Every Monday at 9am", value: "0 9 * * 1" },
  { label: "Every 15 minutes", value: "*/15 * * * *" },
  { label: "First day of month at 3am", value: "0 3 1 * *" },
];

export function CronScheduler({ workflow }: CronSchedulerProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();

  const [cronExpression, setCronExpression] = useState<string>(
    workflow.cron_expression || "",
  );
  const [timezone, setTimezone] = useState<string>(workflow.timezone || "UTC");
  const [enabled, setEnabled] = useState<boolean>(
    workflow.cron_enabled || false,
  );

  const updateScheduleMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.put(
        `/v1/workflows/scheduler/${workflow.workflow_id}`,
        {
          cron_expression: cronExpression,
          timezone,
          cron_enabled: enabled,
        },
      );
      return response.data;
    },
    onSuccess: () => {
      toast({
        title: "Schedule updated",
        description: enabled
          ? "Workflow has been scheduled successfully"
          : "Workflow schedule has been disabled",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflow", workflow.workflow_permanent_id],
      });
    },
    onError: (error) => {
      toast({
        title: "Failed to update schedule",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  const handleApplyExample = (example: string) => {
    setCronExpression(example);
  };

  const handleSave = () => {
    updateScheduleMutation.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ClockIcon className="h-5 w-5" />
          Schedule Workflow
        </CardTitle>
        <CardDescription>
          Set up a schedule to run this workflow automatically at specified
          times
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <Label htmlFor="cron-enabled" className="text-sm font-medium">
              Enable Scheduling
            </Label>
            <Switch
              id="cron-enabled"
              checked={enabled}
              onCheckedChange={setEnabled}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="cron-expression">Cron Expression</Label>
            <Input
              id="cron-expression"
              placeholder="0 9 * * 1-5"
              value={cronExpression}
              onChange={(e) => setCronExpression(e.target.value)}
              disabled={!enabled}
            />
            <p className="text-xs text-muted-foreground">
              Use cron syntax to specify when the workflow should run
            </p>
          </div>

          <div className="space-y-2">
            <Label>Quick Examples</Label>
            <div className="flex flex-wrap gap-2">
              {CRON_EXAMPLES.map((example) => (
                <Button
                  key={example.value}
                  variant="outline"
                  size="sm"
                  onClick={() => handleApplyExample(example.value)}
                  disabled={!enabled}
                >
                  {example.label}
                </Button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="timezone">Timezone</Label>
            <Select
              value={timezone}
              onValueChange={setTimezone}
              disabled={!enabled}
            >
              <SelectTrigger id="timezone">
                <SelectValue placeholder="Select timezone" />
              </SelectTrigger>
              <SelectContent>
                {TIMEZONES.map((tz) => (
                  <SelectItem key={tz} value={tz}>
                    {tz}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {workflow.next_run_time && enabled && (
            <div className="rounded-md bg-muted p-3">
              <p className="text-sm">
                <strong>Next scheduled run:</strong>{" "}
                {new Date(workflow.next_run_time).toLocaleString()}
              </p>
            </div>
          )}

          <Button
            onClick={handleSave}
            className="w-full"
            disabled={enabled && !cronExpression.trim()}
          >
            {updateScheduleMutation.isPending ? "Saving..." : "Save Schedule"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
