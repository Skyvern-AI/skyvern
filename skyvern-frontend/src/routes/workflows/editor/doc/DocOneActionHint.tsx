import { Cross2Icon } from "@radix-ui/react-icons";
import { useContext, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import type { AppNode } from "../nodes";
import { announceDoc } from "./docAnnounce";
import { NO_START_PAGE_REASON } from "./docFailureText";
import { docOneActionStepWantsTask } from "./docOneAction";
import { docShortUrl, type DocPromptUrlOffer } from "./docPromptUrl";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

// Dismissals outlive the component: switching an If branch unmounts its steps' hints.
const dismissed = new Set<string>();

// One suggestion under a step at a time: the Browser Task hint first, then the prompt's URL for an empty URL field.
export function DocOneActionHint({
  node,
  urlOffer = null,
}: {
  node: AppNode;
  urlOffer?: DocPromptUrlOffer | null;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const { edit } = actions;
  const [, rerender] = useState(0);
  if (!edit) return null;
  // A dismissed suggestion's button leaves the page, so focus goes back to the step's prompt line.
  const dismiss = (key: string) => () => {
    dismissed.add(key);
    rerender((n) => n + 1);
    requestAnimationFrame(() => actions.setFocusId(node.id));
  };
  const oneActionKey = `one-action:${node.id}`;
  if (!dismissed.has(oneActionKey) && docOneActionStepWantsTask(node))
    return (
      <DocSuggestion
        key={oneActionKey}
        kind="one-action"
        reason="This step does one action. Turn it into a Browser Task to do several?"
        label="Turn into Browser Task"
        onApply={() => edit.turnInto(node.id, "navigation")}
        onDismiss={dismiss(oneActionKey)}
      />
    );
  const urlKey = urlOffer && `url:${node.id}:${urlOffer.url}`;
  if (!urlOffer || !urlKey || dismissed.has(urlKey)) return null;
  const { url } = urlOffer;
  return (
    <DocSuggestion
      key={urlKey}
      kind="url"
      reason={
        urlOffer.required
          ? NO_START_PAGE_REASON
          : "Without a URL, this step starts on the previous step's page."
      }
      label={`Use ${docShortUrl(url)} as the URL`}
      title={url}
      onApply={() => {
        edit.setText(node.id, "url", url);
        announceDoc(`URL set to ${url}`);
        requestAnimationFrame(() => actions.setFocusId(node.id));
      }}
      onDismiss={dismiss(urlKey)}
    />
  );
}

function DocSuggestion({
  kind,
  reason,
  label,
  title,
  onApply,
  onDismiss,
}: {
  kind: string;
  reason: string;
  label: string;
  title?: string;
  onApply: () => void;
  onDismiss: () => void;
}) {
  const reasonId = useId();
  return (
    <div
      data-doc-suggestion={kind}
      className="mb-1 mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted-foreground"
    >
      <span id={reasonId}>{reason}</span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        title={title}
        aria-describedby={reasonId}
        className="h-6 max-w-full px-2 text-[12.5px]"
        onClick={onApply}
      >
        {label}
      </Button>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        className="size-6"
        aria-label="Dismiss"
        onClick={onDismiss}
      >
        <Cross2Icon className="size-3.5" />
      </Button>
    </div>
  );
}
