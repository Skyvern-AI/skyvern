import type { WorkflowBlockImage } from "../../types/workflowTypes";

// A screenshot still uploading is reserved in the step's docImages under a handle, so every edit and undo moves it
// with its line. The prefix never matches the backend's ^file_[0-9]{1,20}$: a handle that reached a save is refused.
const HANDLE_PREFIX = "pending_";

export const newDocImageHandle = () => `${HANDLE_PREFIX}${crypto.randomUUID()}`;

export const isDocImageHandle = (id: string) => id.startsWith(HANDLE_PREFIX);

// Never cleared: an undo can bring a handle back long after its upload finished.
const uploaded = new Map<string, string>();

export function resolveDocImageHandle(handle: string, fileId: string) {
  uploaded.set(handle, fileId);
}

/** The file an entry shows: its own id, the file its finished upload got, or undefined while not uploaded. */
export const docImageFileId = (id: string): string | undefined =>
  isDocImageHandle(id) ? uploaded.get(id) : id;

/** The entries a save sends: finished uploads as their files, the rest left out. */
export function resolveDocImages(
  images: WorkflowBlockImage[],
): WorkflowBlockImage[] {
  return images.flatMap((image) => {
    const fileId = docImageFileId(image.file_id);
    return fileId ? [{ ...image, file_id: fileId }] : [];
  });
}
