import { useContext, useEffect, useRef, useState } from "react";

import { toast } from "@/components/ui/use-toast";
import { cn } from "@/util/utils";

import type { WorkflowBlockNode } from "../nodes";
import { docStepCaption } from "./docStepRegistry";
import {
  docNameLabelPreview,
  docNameLabelProblem,
  docNameToLabel,
} from "./docStepNameLabel";
import { useDocGoalBuild } from "./useDocGoalBuild";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

const PLACEHOLDER = "Name this step";
const DEFAULT_NAME = "Add a word: this name reads as a default one";
// What was typed, by node id, so an entry marks the field as touched. A Copilot proposal replaces the live steps, so a
// draft outlives its field here; node ids are generated per load, so none carries to another workflow.
const drafts = new Map<string, string>();
const title =
  "mt-0.5 w-full break-words text-left text-lg font-semibold leading-snug";

/** A step's title; where the step is editable, click it, or "Name this step", and type: the name becomes its label. */
export function DocStepName({
  node,
  editable,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
}) {
  const { edit } = useContext(WorkflowDocActionsContext);
  const goalBuild = useDocGoalBuild(node.data.label);
  const caption = docStepCaption(node);
  // A lock (a save, a proposal, a demonstration, a build) keeps the draft and saves nothing until it lifts.
  const locked =
    !editable || !edit || (node.type === "codeBlock" && goalBuild !== null);
  const [draft, setDraftState] = useState<string | null>(
    () => drafts.get(node.id) ?? null,
  );
  const setDraft = (next: string | null, typed = false) => {
    if (next === null) drafts.delete(node.id);
    else if (typed) drafts.set(node.id, next);
    setDraftState(next);
  };
  const [problem, setProblem] = useState<string | null>(null);
  const titleRef = useRef<HTMLButtonElement>(null);
  // Set by Enter and Escape: focus returns to the title, and the blur that follows the unmount does nothing.
  const keyboardExit = useRef(false);
  useEffect(() => {
    if (draft === null && keyboardExit.current) titleRef.current?.focus();
  }, [draft]);
  const hintId = `${node.id}-name-hint`;
  if (locked && draft === null)
    return caption ? (
      <h3 className={cn(title, "text-foreground")}>{caption}</h3>
    ) : null;
  const close = () => {
    setDraft(null);
    setProblem(null);
  };
  if (draft === null)
    return (
      <h3 className="mt-0.5">
        <button
          type="button"
          ref={titleRef}
          data-doc-step-name
          aria-describedby={caption ? hintId : undefined}
          onClick={() => {
            keyboardExit.current = false;
            setDraft(caption ?? "");
          }}
          className={cn(
            title,
            "mt-0 rounded outline-none focus-visible:ring-2 focus-visible:ring-ring",
            caption
              ? "text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {caption ?? PLACEHOLDER}
        </button>
        {/* The heading's name stays the title, as in a read-only Doc; Rename is the button's description. */}
        {caption && (
          <span id={hintId} hidden>
            Rename
          </span>
        )}
      </h3>
    );
  // Enter keeps the field open on a problem; a blur reverts and says why.
  const commit = (fromBlur: boolean) => {
    if (locked || !edit) return;
    // A lossy title (Review_v2 reads as Review) would rename the step if an untouched field were saved.
    if (!drafts.has(node.id)) return close();
    const label = docNameToLabel(draft);
    const old = node.data.label;
    const readBack = label
      ? docStepCaption({ ...node, data: { ...node.data, label } })
      : null;
    // The title as it reads already, whatever the label's case, leaves the label unchanged.
    if (
      !draft.trim() ||
      label === old ||
      (readBack === caption && label.toLowerCase() === old.toLowerCase())
    )
      return close();
    const error =
      docNameLabelProblem(label) ??
      (readBack === null
        ? DEFAULT_NAME
        : edit.rename(node.id, label, { fromTitle: true }));
    if (error && !fromBlur) {
      keyboardExit.current = false;
      return setProblem(error);
    }
    if (error)
      toast({
        variant: "destructive",
        title: "Name not saved",
        description: error,
      });
    close();
  };
  const preview = docNameLabelPreview(draft);
  return (
    <div className="mt-0.5 flex flex-col">
      <input
        autoFocus
        aria-label="Step name"
        aria-invalid={Boolean(problem)}
        aria-describedby={
          problem || preview ? `${node.id}-name-note` : undefined
        }
        placeholder={PLACEHOLDER}
        readOnly={locked}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value, true);
          setProblem(null);
        }}
        onKeyDown={(e) => {
          // An input method's Enter or Escape confirms or cancels its candidate, not the name.
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          if (e.key === "Enter") {
            e.preventDefault();
            e.stopPropagation();
            if (locked) return;
            keyboardExit.current = true;
            commit(false);
          }
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            keyboardExit.current = true;
            close();
          }
        }}
        onBlur={(e) => {
          // A field taken off screen by a proposal keeps its draft rather than saving it.
          if (!keyboardExit.current && e.currentTarget.isConnected)
            commit(true);
        }}
        className={cn(
          "w-full rounded border border-input bg-background px-1 text-lg font-semibold leading-snug text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring",
          problem && "border-destructive",
        )}
      />
      {problem ? (
        <span
          id={`${node.id}-name-note`}
          role="alert"
          className="text-xs text-destructive"
        >
          {problem}
        </span>
      ) : (
        preview && (
          <span
            id={`${node.id}-name-note`}
            className="break-all text-xs text-muted-foreground"
          >
            Saves as {preview}
          </span>
        )
      )}
    </div>
  );
}
