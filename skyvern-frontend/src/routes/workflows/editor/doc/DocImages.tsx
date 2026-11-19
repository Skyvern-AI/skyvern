import {
  useContext,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { Cross2Icon, ImageIcon } from "@radix-ui/react-icons";

import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { localArtifactImageURL } from "@/routes/tasks/detail/artifactUtils";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { cn } from "@/util/utils";
import type { WorkflowBlockImage } from "../../types/workflowTypes";
import { useWorkflowScopeId } from "../WorkflowScopeContext";
import { announceDoc } from "./docAnnounce";
import { docImageFileId } from "./docImageHandles";
import {
  DOC_IMAGE_TYPES,
  localImageUrl,
  mintDocImageUrl,
  uploadedOnThisPage,
  useDocImageUploadStore,
  type DocImageSignedUrl,
  type DocImageSlot,
  type DocImageUpload,
} from "./docImageUploads";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

const PEOPLE_ONLY =
  "For people reading the workflow; the agent does not see it.";

// Where a screenshot sits, for the names of its controls: "the note above step 3", "step 3".
function useDocImagePlace(slot: DocImageSlot): string {
  const { stepNumbers } = useContext(WorkflowDocActionsContext);
  const number = stepNumbers.get(slot.ownerNodeId);
  const step = number ? `step ${number}` : "this step";
  if (slot.placement === "block") return step;
  return `the note ${slot.placement === "above" ? "above" : "after"} ${step}`;
}

// A press that blurs the line would end a staged note, which moves the row away before the click lands.
const keepLineFocus = (event: MouseEvent) => event.preventDefault();

export function DocImagePicker({
  slot,
  onFiles,
  className,
}: {
  slot: DocImageSlot;
  onFiles: (files: File[]) => void;
  className?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const place = useDocImagePlace(slot);
  return (
    <>
      <button
        type="button"
        aria-label={`Add a screenshot to ${place}`}
        title={`Add a screenshot (or paste or drop one). ${PEOPLE_ONLY}`}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => input.current?.click()}
        className={cn(
          "flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
          className,
        )}
      >
        <ImageIcon aria-hidden className="size-3.5" />
      </button>
      <input
        ref={input}
        type="file"
        hidden
        multiple
        accept={DOC_IMAGE_TYPES.join(",")}
        data-doc-image-input
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          onFiles(files);
        }}
      />
    </>
  );
}

// A reservation whose upload this page no longer runs, such as one an undo brought back after the editor reloaded.
const LOST_UPLOAD_TEXT = "This screenshot didn't finish uploading.";

/** The screenshots under one line: each entry is its tile, or the row of the upload that reserved it. */
export function DocImages({
  slot,
  images,
  editable,
  onRetry,
}: {
  slot: DocImageSlot;
  images: WorkflowBlockImage[];
  editable: boolean;
  onRetry: (upload: DocImageUpload) => void;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const root = useRef<HTMLDivElement>(null);
  const place = useDocImagePlace(slot);
  // Subscribed so a finished upload, which resolves its handle and drops its row, renders its tile.
  const uploads = useDocImageUploadStore((state) => state.uploads);
  if (!images.length) return null;
  const canEdit = editable && Boolean(actions.edit);
  const entries = images.map((image) => {
    const fileId = docImageFileId(image.file_id);
    return fileId
      ? { image: { ...image, file_id: fileId }, key: image.file_id }
      : {
          key: image.file_id,
          upload: uploads.find((u) => u.id === image.file_id),
        };
  });
  const tiles = entries.filter((entry) => entry.image).length;
  // The button pressed can leave with its row; the line the row sits under keeps the focus. The line is found
  // after the change, as leaving it swaps its editing element for the view.
  const thenFocusLine = (change: () => void) => {
    const container = root.current?.parentElement;
    change();
    requestAnimationFrame(() => {
      if (container?.isConnected)
        container.querySelector<HTMLElement>("[data-doc-line]")?.focus();
    });
  };
  // Removing an entry is one undo step, whether it shows a screenshot or an upload.
  const remove = (entryId: string, said: string) =>
    thenFocusLine(() => {
      actions.edit?.removeImage(slot.ownerNodeId, entryId);
      announceDoc(said);
    });
  let shown = 0;
  return (
    <div
      ref={root}
      data-doc-images
      className="mt-1.5 flex flex-col items-start gap-2"
    >
      {entries.map((entry) => {
        if (entry.image) {
          shown += 1;
          return (
            <DocImageTile
              key={entry.key}
              image={entry.image}
              name={`screenshot ${shown} of ${tiles}`}
              place={place}
              onRemove={
                canEdit
                  ? () => remove(entry.key, "Screenshot removed")
                  : undefined
              }
            />
          );
        }
        const upload = entry.upload;
        const uploading = upload?.status === "uploading";
        return (
          <div
            key={entry.key}
            role={uploading ? "status" : "alert"}
            className={cn(
              "flex max-w-full items-center gap-2 rounded-md border px-2.5 py-1.5 text-[13px]",
              uploading
                ? "border-border text-muted-foreground"
                : "border-destructive/40 text-destructive",
            )}
          >
            <span className="min-w-0 [overflow-wrap:anywhere]">
              {uploading
                ? `Uploading ${upload.name || "screenshot"}…`
                : (upload?.message ?? LOST_UPLOAD_TEXT)}
            </span>
            {upload?.status === "failed" && upload.file && (
              <button
                type="button"
                onMouseDown={keepLineFocus}
                onClick={() => thenFocusLine(() => onRetry(upload))}
                className="shrink-0 rounded px-1 text-foreground underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Retry
              </button>
            )}
            {canEdit && (
              <button
                type="button"
                aria-label="Dismiss"
                onMouseDown={keepLineFocus}
                onClick={() => remove(entry.key, "Screenshot dismissed")}
                className="shrink-0 rounded p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Cross2Icon aria-hidden className="size-3.5" />
              </button>
            )}
          </div>
        );
      })}
      {tiles > 0 && (
        <p className="text-xs text-muted-foreground">{PEOPLE_ONLY}</p>
      )}
    </div>
  );
}

const gone = (error: unknown) =>
  isAxiosError(error) && error.response?.status === 404;

// The signed-url route's own words for every refusal.
const DOC_IMAGE_GONE_TEXT = "File is no longer available";
const UNSAVED_TEXT = "This screenshot shows after the workflow is saved.";

const isLocalUri = (url: string) => url.startsWith("file://");

// The local backend links to file:// URIs, which an <img> can't load; the artifact server reads them.
const imageSrc = (data: DocImageSignedUrl) =>
  isLocalUri(data.signed_url)
    ? localArtifactImageURL(data.signed_url)
    : data.signed_url;

function DocImageTile({
  image,
  name,
  place,
  onRemove,
}: {
  image: WorkflowBlockImage;
  name: string;
  place: string;
  onRemove?: () => void;
}) {
  const credentialGetter = useCredentialGetter();
  const workflowId = useWorkflowScopeId();
  const hasChanges = useWorkflowHasChangesStore((state) => state.hasChanges);
  const local = localImageUrl(image.file_id);
  const query = useQuery({
    queryKey: ["docImageSignedUrl", workflowId, image.file_id],
    queryFn: () =>
      mintDocImageUrl(credentialGetter, image.file_id, workflowId!),
    enabled: !local && Boolean(workflowId),
    // Signed links last an hour and are minted again on a load error; a link with no expiry is kept.
    staleTime: (q) =>
      q.state.data && q.state.data.expires_at === null
        ? Infinity
        : 50 * 60 * 1000,
    retry: (count, error) => !gone(error) && count < 1,
  });
  // A 404 for an image added on this page, before a save, is the saved definition not naming it yet; asked again
  // once the save lands.
  const refusedHere =
    !local &&
    query.isError &&
    gone(query.error) &&
    uploadedOnThisPage(image.file_id);
  const unsaved = refusedHere && hasChanges;
  const { refetch } = query;
  useEffect(() => {
    if (refusedHere && !hasChanges) void refetch();
  }, [refusedHere, hasChanges, refetch]);
  // One fresh link per failure; a second failure in a row is shown instead of retried.
  const [reminted, setReminted] = useState(false);
  const [broken, setBroken] = useState(false);
  const src = local ?? (query.data ? imageSrc(query.data) : undefined);
  // A new link gets a fresh try. `reminted` stays: each mint is a new link, so clearing it would re-mint forever.
  const [triedSrc, setTriedSrc] = useState(src);
  if (triedSrc !== src) {
    setTriedSrc(src);
    setBroken(false);
  }
  const alt = image.caption?.trim() || name;
  let problem: string | null = null;
  if (!local && !workflowId) problem = UNSAVED_TEXT;
  else if (!local && query.isError)
    problem = unsaved
      ? UNSAVED_TEXT
      : gone(query.error)
        ? DOC_IMAGE_GONE_TEXT
        : "Couldn't load this screenshot.";
  else if (broken) problem = "Couldn't load this screenshot.";
  return (
    <figure className="group relative m-0 max-w-[min(100%,480px)] overflow-hidden rounded-md border border-border bg-muted/40">
      {problem ? (
        <figcaption
          role="status"
          className="px-3 py-2 pr-9 text-[13px] text-muted-foreground"
        >
          {problem}
        </figcaption>
      ) : src ? (
        <img
          src={src}
          alt={alt}
          className="block max-h-[360px] max-w-full object-contain"
          onLoad={() => setReminted(false)}
          onError={() => {
            // A file:// link never expires, so a fresh one would fail the same way.
            if (
              local ||
              reminted ||
              !query.data ||
              isLocalUri(query.data.signed_url)
            ) {
              setBroken(true);
              return;
            }
            setReminted(true);
            void query.refetch().then((result) => {
              if (!result.data || imageSrc(result.data) === src)
                setBroken(true);
            });
          }}
        />
      ) : (
        <div
          role="status"
          className="px-3 py-2 pr-9 text-[13px] text-muted-foreground"
        >
          Loading screenshot…
        </div>
      )}
      {onRemove && (
        <button
          type="button"
          aria-label={`Remove ${name} from ${place}`}
          onMouseDown={keepLineFocus}
          onClick={onRemove}
          className="absolute right-1 top-1 flex size-6 items-center justify-center rounded bg-background/90 text-muted-foreground opacity-0 shadow-sm outline-none hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 [@media(hover:none)]:opacity-100"
        >
          <Cross2Icon aria-hidden className="size-3.5" />
        </button>
      )}
    </figure>
  );
}
