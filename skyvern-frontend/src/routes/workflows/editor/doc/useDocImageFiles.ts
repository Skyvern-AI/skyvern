import { useContext, useRef, type DragEvent } from "react";

import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowDocImagesFlag } from "@/hooks/useWorkflowDocEditorFlag";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { reconcileYamlDraftAfterGraphChange } from "@/store/WorkflowYamlEditorStore";
import { announceDoc } from "./docAnnounce";
import { DOC_IMAGES_CAP_MESSAGE, DOC_IMAGES_MAX } from "./docGraphEdits";
import { newDocImageHandle, resolveDocImageHandle } from "./docImageHandles";
import {
  docImageGeneration,
  docImageRefusal,
  fitDocImage,
  hasFiles,
  rememberLocalImage,
  uploadDocImage,
  useDocImageUploadStore,
  whenNoSaveOrYamlView,
  type DocImageSlot,
  type DocImageUpload,
} from "./docImageUploads";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

// Whether the live graph names the file; a done handle is saved as its file, so the save data shows it. Save data
// that can't be built is read as naming it, so an unsaved mark is never lost.
const BELOW_TEXT = "Screenshots go under the step's text.";

const inLiveGraph = (fileId: string) => {
  try {
    const blocks = useWorkflowHasChangesStore.getState().getSaveData()?.blocks;
    return Boolean(blocks && JSON.stringify(blocks).includes(`"${fileId}"`));
  } catch {
    return true;
  }
};

/**
 * Screenshots for a slot: each accepted file is reserved in the step at once, as one undo step, and the reservation
 * moves with its line through any edit. A finished upload only resolves the reservation; it never edits the graph.
 */
export function useDocImageFiles(slot: DocImageSlot) {
  const actions = useContext(WorkflowDocActionsContext);
  const credentialGetter = useCredentialGetter();
  const enabled = useWorkflowDocImagesFlag();
  // An upload outlives the line that started it: the line can move, or the Doc can unmount.
  const edit = useRef(actions.edit);
  edit.current = actions.edit;
  const { put, drop } = useDocImageUploadStore.getState();

  const fail = (upload: DocImageUpload, message: string, file?: File) => {
    put({ ...upload, status: "failed", message, file });
    announceDoc(message);
  };

  const refuse = (message: string) => {
    toast({ title: message, variant: "destructive" });
    announceDoc(message);
  };

  // `fitted` waits for the file before it in the same add, so one image is decoded at a time.
  const send = async (
    upload: DocImageUpload,
    file: File,
    fitted: Promise<File | string> = fitDocImage(file),
  ) => {
    put({ ...upload, status: "uploading", message: undefined, file });
    const started = docImageGeneration();
    // An editor that went away takes nothing more.
    const live = () => docImageGeneration() === started;
    const fit = await fitted;
    if (!live()) return;
    if (typeof fit === "string") return fail(upload, fit);
    let fileId: string;
    try {
      fileId = await uploadDocImage(credentialGetter, fit);
    } catch {
      if (live())
        fail(
          upload,
          `Couldn't upload ${file.name || "the screenshot"}. Your text is kept.`,
          fit,
        );
      return;
    }
    if (!live()) return;
    rememberLocalImage(fileId, fit);
    // Resolved before the row goes, so the render that drops the row shows the tile.
    resolveDocImageHandle(upload.id, fileId);
    drop(upload.id);
    // Dismissed, undone, or replaced by a Copilot accept, a restore or a YAML commit: nothing to announce.
    if (!inLiveGraph(fileId)) return;
    announceDoc("Screenshot added");
    // The file is new save data that no graph change announces: an open YAML draft lacks it, and the unsaved mark
    // may have been cleared while the draft matched the saved workflow without it.
    reconcileYamlDraftAfterGraphChange();
    await whenNoSaveOrYamlView();
    if (!live() || !inLiveGraph(fileId)) return;
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
  };

  // The toast shows one message at a time, so the reasons of one add go in one.
  const report = (refusals: string[], below: boolean) => {
    if (refusals.length)
      refuse([...refusals, ...(below ? [BELOW_TEXT] : [])].join(" "));
    else if (below)
      toast({
        title: "Screenshot added below the step's text",
        description: "Screenshots sit under the text, not inside it.",
      });
  };

  const addFiles = (files: File[]) => {
    const api = edit.current;
    if (!api || !files.length) return;
    const room = DOC_IMAGES_MAX - api.imageCount(slot.ownerNodeId);
    const accepted: Array<{ upload: DocImageUpload; file: File }> = [];
    const refusals: string[] = [];
    for (const file of files) {
      const refusal = docImageRefusal(file);
      if (refusal) refusals.push(refusal);
      else if (accepted.length < room)
        accepted.push({
          file,
          upload: {
            id: newDocImageHandle(),
            name: file.name,
            status: "uploading",
          },
        });
      else if (!refusals.includes(DOC_IMAGES_CAP_MESSAGE))
        refusals.push(DOC_IMAGES_CAP_MESSAGE);
    }
    if (!accepted.length) return report(refusals, false);
    for (const { upload } of accepted) put(upload);
    void reserve(api, accepted, refusals);
  };

  const reserve = async (
    api: NonNullable<typeof edit.current>,
    accepted: Array<{ upload: DocImageUpload; file: File }>,
    refusals: string[],
  ) => {
    const outcome = await api.addImages(
      slot.ownerNodeId,
      accepted.map(({ upload }) => ({
        file_id: upload.id,
        caption: null,
        placement: slot.placement,
      })),
    );
    if (outcome !== "added") {
      for (const { upload } of accepted) drop(upload.id);
      // Another add filled the step before this one reached the graph.
      if (outcome === "full" && !refusals.includes(DOC_IMAGES_CAP_MESSAGE))
        refusals.push(DOC_IMAGES_CAP_MESSAGE);
      return report(refusals, false);
    }
    report(refusals, slot.placement === "block");
    let fits: Promise<unknown> = Promise.resolve();
    for (const { upload, file } of accepted) {
      const fitted = fits.then(() => fitDocImage(file));
      fits = fitted.catch(() => undefined);
      void send(upload, file, fitted);
    }
  };

  // The reservation still holds its place, so a retry needs no room.
  const retry = (upload: DocImageUpload) => {
    if (upload.file) void send(upload, upload.file);
  };

  return { addFiles: enabled ? addFiles : null, retry };
}

/** Drag and drop of files onto a line; anything else dragged keeps its usual meaning. */
export function docImageDropProps(onFiles: ((files: File[]) => void) | null) {
  if (!onFiles) return {};
  return {
    onDragOver: (event: DragEvent) => {
      if (!hasFiles(event.dataTransfer)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    },
    onDrop: (event: DragEvent) => {
      if (!hasFiles(event.dataTransfer)) return;
      event.preventDefault();
      event.stopPropagation();
      onFiles(Array.from(event.dataTransfer.files));
    },
  };
}
