import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";

import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrowserSessionsQuery } from "@/routes/browserSessions/hooks/useBrowserSessionsQuery";
import { useRunsQuery } from "@/hooks/useRunsQuery";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";
import { Task, WorkflowRunApiResponse } from "@/api/types";
import { useCreateBrowserProfileMutation } from "./hooks/useCreateBrowserProfileMutation";
import { basicLocalTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";

type SourceType = "browserSession" | "workflowRun";

type CreateBrowserProfileFormValues = {
  name: string;
  description: string;
  sourceType: SourceType;
  browserSessionId: string | null;
  workflowRunId: string | null;
};

type CreateBrowserProfileDrawerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

function isWorkflowRun(
  run: Task | WorkflowRunApiResponse,
): run is WorkflowRunApiResponse {
  return "workflow_run_id" in run;
}

function renderSessionLabel(session: BrowserSession) {
  const isOpen = session.completed_at === null && session.started_at !== null;

  return (
    <div className="flex flex-col">
      <div className="font-mono text-xs">{session.browser_session_id}</div>
      <div className="text-xs text-slate-500">
        {isOpen
          ? "Open session"
          : session.started_at
            ? `Started ${basicLocalTimeFormat(session.started_at)}`
            : "Not started"}
      </div>
    </div>
  );
}

function renderWorkflowRunLabel(run: WorkflowRunApiResponse) {
  return (
    <div className="flex flex-col">
      <div className="font-mono text-xs">{run.workflow_run_id}</div>
      <div className="text-xs text-slate-500">
        {run.workflow_title ?? "Untitled workflow"} •{" "}
        {basicLocalTimeFormat(run.created_at)}
      </div>
    </div>
  );
}

function CreateBrowserProfileDrawer({
  open,
  onOpenChange,
}: CreateBrowserProfileDrawerProps) {
  const form = useForm<CreateBrowserProfileFormValues>({
    defaultValues: {
      name: "",
      description: "",
      sourceType: "browserSession",
      browserSessionId: null,
      workflowRunId: null,
    },
  });

  const createMutation = useCreateBrowserProfileMutation();
  const { data: browserSessions, isLoading: sessionsLoading } =
    useBrowserSessionsQuery(1, 20);
  const { data: runs, isLoading: runsLoading } = useRunsQuery({
    page: 1,
    pageSize: 20,
  });

  const [sessionSearch, setSessionSearch] = useState("");
  const [workflowRunSearch, setWorkflowRunSearch] = useState("");

  const workflowRuns = useMemo(
    () => (runs ?? []).filter(isWorkflowRun),
    [runs],
  );

  const filteredSessions = useMemo(() => {
    if (!browserSessions) return [];
    console.log(
      "[CreateBrowserProfile] Total sessions:",
      browserSessions.length,
    );
    // Only show closed sessions (they have persisted state)
    const closedSessions = browserSessions.filter(
      (session) => session.completed_at !== null,
    );
    console.log(
      "[CreateBrowserProfile] Closed sessions:",
      closedSessions.length,
      closedSessions.map((s) => ({
        id: s.browser_session_id,
        completed_at: s.completed_at,
      })),
    );
    const normalized = sessionSearch.trim().toLowerCase();
    if (!normalized) return closedSessions;
    return closedSessions.filter((session) =>
      session.browser_session_id.toLowerCase().includes(normalized),
    );
  }, [browserSessions, sessionSearch]);

  const filteredWorkflowRuns = useMemo(() => {
    const normalized = workflowRunSearch.trim().toLowerCase();
    if (!normalized) return workflowRuns;
    return workflowRuns.filter(
      (run) =>
        run.workflow_run_id.toLowerCase().includes(normalized) ||
        (run.workflow_title ?? "").toLowerCase().includes(normalized),
    );
  }, [workflowRuns, workflowRunSearch]);

  const sourceType = form.watch("sourceType");
  const selectedSessionId = form.watch("browserSessionId");
  const selectedWorkflowRunId = form.watch("workflowRunId");

  function closeAndReset() {
    onOpenChange(false);
    form.reset({
      name: "",
      description: "",
      sourceType: "browserSession",
      browserSessionId: null,
      workflowRunId: null,
    });
    setSessionSearch("");
    setWorkflowRunSearch("");
  }

  useEffect(() => {
    if (!open) {
      closeAndReset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const isSubmitting = createMutation.isPending;

  function onSubmit(values: CreateBrowserProfileFormValues) {
    console.log("[CreateBrowserProfile] Form values:", values);

    if (!values.name.trim()) {
      form.setError("name", { message: "Name is required" });
      return;
    }

    if (values.sourceType === "browserSession") {
      if (!values.browserSessionId) {
        form.setError("browserSessionId", {
          message: "Select a browser session",
        });
        return;
      }

      console.log(
        "[CreateBrowserProfile] Creating from browser session:",
        values.browserSessionId,
      );
      createMutation.mutate(
        {
          name: values.name.trim(),
          description: values.description.trim() || null,
          browserSessionId: values.browserSessionId,
        },
        {
          onSuccess: closeAndReset,
        },
      );
      return;
    }

    if (!values.workflowRunId) {
      form.setError("workflowRunId", {
        message: "Select a workflow run",
      });
      return;
    }

    console.log(
      "[CreateBrowserProfile] Creating from workflow run:",
      values.workflowRunId,
    );
    createMutation.mutate(
      {
        name: values.name.trim(),
        description: values.description.trim() || null,
        workflowRunId: values.workflowRunId,
      },
      {
        onSuccess: closeAndReset,
      },
    );
  }

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction="right">
      <DrawerContent className="bottom-2 right-0 top-2 mt-0 h-full w-full max-w-xl rounded-none border-0 p-0 shadow-2xl">
        <div className="flex h-full flex-col">
          <DrawerHeader className="border-b px-6 py-4">
            <DrawerTitle>Create Browser Profile</DrawerTitle>
            <DrawerDescription>
              Persist browser state from an existing session or workflow run for
              future reuse.
            </DrawerDescription>
          </DrawerHeader>
          <div className="flex flex-1 flex-col overflow-hidden px-6 py-4">
            <form
              className="flex h-full flex-col gap-6"
              onSubmit={form.handleSubmit(onSubmit)}
            >
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="profile-name">Name</Label>
                  <Input
                    id="profile-name"
                    placeholder="Marketing workspace login"
                    {...form.register("name")}
                  />
                  {form.formState.errors.name ? (
                    <p className="text-sm text-destructive">
                      {form.formState.errors.name.message}
                    </p>
                  ) : null}
                </div>
                <div className="space-y-2">
                  <Label htmlFor="profile-description">Description</Label>
                  <Textarea
                    id="profile-description"
                    placeholder="Optional description"
                    rows={3}
                    {...form.register("description")}
                  />
                </div>
              </div>

              <div className="flex-1 overflow-hidden">
                <Tabs
                  value={sourceType}
                  onValueChange={(value) => {
                    const newSourceType = value as SourceType;
                    form.setValue("sourceType", newSourceType);
                    // Clear selection from the inactive tab
                    if (newSourceType === "browserSession") {
                      form.setValue("workflowRunId", null);
                    } else {
                      form.setValue("browserSessionId", null);
                    }
                  }}
                  className="flex h-full flex-col"
                >
                  <TabsList className="w-full">
                    <TabsTrigger value="browserSession" className="flex-1">
                      Browser Session
                    </TabsTrigger>
                    <TabsTrigger value="workflowRun" className="flex-1">
                      Workflow Run
                    </TabsTrigger>
                  </TabsList>
                  <TabsContent
                    value="browserSession"
                    className="flex flex-1 flex-col overflow-hidden pt-4"
                  >
                    <div className="space-y-2">
                      <Label>Choose a closed browser session</Label>
                      <p className="text-xs text-slate-500">
                        Only closed sessions with persisted state can be saved
                        as profiles.
                      </p>
                      <Input
                        placeholder="Filter by session ID…"
                        value={sessionSearch}
                        onChange={(event) =>
                          setSessionSearch(event.target.value)
                        }
                      />
                    </div>
                    <div className="mt-3 flex flex-1 flex-col overflow-hidden">
                      {sessionsLoading ? (
                        <div className="space-y-2">
                          <Skeleton className="h-10 w-full" />
                          <Skeleton className="h-10 w-full" />
                          <Skeleton className="h-10 w-full" />
                        </div>
                      ) : filteredSessions.length === 0 ? (
                        <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                          No browser sessions found.
                        </div>
                      ) : (
                        <ScrollArea className="flex-1 rounded border">
                          <RadioGroup
                            value={selectedSessionId ?? ""}
                            onValueChange={(value) =>
                              form.setValue(
                                "browserSessionId",
                                value === selectedSessionId ? null : value,
                              )
                            }
                            className="flex flex-col"
                          >
                            {filteredSessions.map((session) => (
                              <label
                                key={session.browser_session_id}
                                className={cn(
                                  "flex cursor-pointer items-center gap-3 border-b px-4 py-3 text-left hover:bg-slate-50 dark:hover:bg-slate-900/40",
                                  selectedSessionId ===
                                    session.browser_session_id &&
                                    "bg-slate-100 dark:bg-slate-900/60",
                                )}
                              >
                                <RadioGroupItem
                                  value={session.browser_session_id}
                                  className="mt-1"
                                />
                                {renderSessionLabel(session)}
                              </label>
                            ))}
                          </RadioGroup>
                        </ScrollArea>
                      )}
                    </div>
                    {form.formState.errors.browserSessionId ? (
                      <p className="mt-2 text-sm text-destructive">
                        {form.formState.errors.browserSessionId.message}
                      </p>
                    ) : null}
                  </TabsContent>
                  <TabsContent
                    value="workflowRun"
                    className="flex flex-1 flex-col overflow-hidden pt-4"
                  >
                    <div className="space-y-2">
                      <Label>Choose a workflow run</Label>
                      <p className="text-xs text-slate-500">
                        Only runs from workflows with "Save & Reuse Session"
                        enabled can be saved as profiles.
                      </p>
                      <Input
                        placeholder="Search by run ID or workflow title…"
                        value={workflowRunSearch}
                        onChange={(event) =>
                          setWorkflowRunSearch(event.target.value)
                        }
                      />
                    </div>
                    <div className="mt-3 flex flex-1 flex-col overflow-hidden">
                      {runsLoading ? (
                        <div className="space-y-2">
                          <Skeleton className="h-10 w-full" />
                          <Skeleton className="h-10 w-full" />
                          <Skeleton className="h-10 w-full" />
                        </div>
                      ) : filteredWorkflowRuns.length === 0 ? (
                        <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                          No workflow runs found.
                        </div>
                      ) : (
                        <ScrollArea className="flex-1 rounded border">
                          <RadioGroup
                            value={selectedWorkflowRunId ?? ""}
                            onValueChange={(value) =>
                              form.setValue(
                                "workflowRunId",
                                value === selectedWorkflowRunId ? null : value,
                              )
                            }
                            className="flex flex-col"
                          >
                            {filteredWorkflowRuns.map((run) => (
                              <label
                                key={run.workflow_run_id}
                                className={cn(
                                  "flex cursor-pointer items-center gap-3 border-b px-4 py-3 text-left hover:bg-slate-50 dark:hover:bg-slate-900/40",
                                  selectedWorkflowRunId ===
                                    run.workflow_run_id &&
                                    "bg-slate-100 dark:bg-slate-900/60",
                                )}
                              >
                                <RadioGroupItem
                                  value={run.workflow_run_id}
                                  className="mt-1"
                                />
                                {renderWorkflowRunLabel(run)}
                              </label>
                            ))}
                          </RadioGroup>
                        </ScrollArea>
                      )}
                    </div>
                    {form.formState.errors.workflowRunId ? (
                      <p className="mt-2 text-sm text-destructive">
                        {form.formState.errors.workflowRunId.message}
                      </p>
                    ) : null}
                  </TabsContent>
                </Tabs>
              </div>

              <DrawerFooter className="flex flex-col gap-2 border-t p-0 pt-4">
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={closeAndReset}
                    disabled={isSubmitting}
                  >
                    Cancel
                  </Button>
                  <Button type="submit" disabled={isSubmitting}>
                    {isSubmitting ? "Saving…" : "Create Profile"}
                  </Button>
                </div>
                <p className="text-xs text-slate-500">
                  Selecting a browser session captures its current archive.
                  Using a workflow run waits for its persisted session archive
                  before creating the profile.
                </p>
              </DrawerFooter>
            </form>
          </div>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

export { CreateBrowserProfileDrawer };
