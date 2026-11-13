import { useEffect } from "react";
import { create } from "zustand";

import { getClient, type CredentialGetter } from "@/api/AxiosClient";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import { ATTACHMENT_SIZE_LIMIT_BYTES } from "../../copilot/attachmentLimits";
import type { WorkflowBlockImage } from "../../types/workflowTypes";

export const DOC_IMAGE_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
] as const;
type DocImageType = (typeof DOC_IMAGE_TYPES)[number];

// The save check's limit for a newly added image.
export const DOC_IMAGE_MAX_BYTES = 4 * 1024 * 1024;

// The save check reads the upload's file name, so it has to name what the bytes are.
const EXTENSIONS: Record<DocImageType, readonly string[]> = {
  "image/png": ["png"],
  "image/jpeg": ["jpg", "jpeg", "jfif"],
  "image/gif": ["gif"],
  "image/webp": ["webp"],
};

const isDocImageType = (type: string): type is DocImageType =>
  (DOC_IMAGE_TYPES as readonly string[]).includes(type);

export type DocImageSlot = {
  ownerNodeId: string;
  placement: WorkflowBlockImage["placement"];
};

// The upload behind a reserved entry, keyed by the entry's handle; it goes when the upload finishes. `file` is kept
// when Retry can send it again.
export type DocImageUpload = {
  id: string;
  name: string;
  status: "uploading" | "failed";
  message?: string;
  file?: File;
};

type DocImageUploadStore = {
  uploads: DocImageUpload[];
  put: (upload: DocImageUpload) => void;
  drop: (id: string) => void;
};

export const useDocImageUploadStore = create<DocImageUploadStore>((set) => ({
  uploads: [],
  put: (upload) =>
    set((state) => ({
      uploads: state.uploads.some((u) => u.id === upload.id)
        ? state.uploads.map((u) => (u.id === upload.id ? upload : u))
        : [...state.uploads, upload],
    })),
  drop: (id) =>
    set((state) => ({ uploads: state.uploads.filter((u) => u.id !== id) })),
}));

// A save or a YAML commit sets the unsaved mark itself, and a change while the YAML view is open marks the YAML stale.
const saveOrYamlBusy = () => {
  const yaml = useWorkflowYamlEditorStore.getState();
  return (
    yaml.active ||
    yaml.commitInProgress ||
    useWorkflowHasChangesStore.getState().saveIsPending
  );
};

// Waits still subscribed; a release ends them, and their callers find the editor gone.
const waiting = new Set<() => void>();

export function whenNoSaveOrYamlView(): Promise<void> {
  if (!saveOrYamlBusy()) return Promise.resolve();
  return new Promise((resolve) => {
    const end = () => {
      stopYaml();
      stopSave();
      waiting.delete(end);
      resolve();
    };
    const check = () => {
      if (!saveOrYamlBusy()) end();
    };
    const stopYaml = useWorkflowYamlEditorStore.subscribe(check);
    const stopSave = useWorkflowHasChangesStore.subscribe(check);
    waiting.add(end);
  });
}

// A just-uploaded image is not in the saved definition yet, so the signed-url route can't serve it until a save.
const localUrls = new Map<string, string>();
// Uploaded on this page; a 404 for one of these while there are unsaved changes means "not saved yet".
// Not cleared by a release: the step can still name the file, unsaved, after its preview is revoked.
const uploadedHere = new Set<string>();
// Bumped when the editor goes away, so an upload that finishes later adds nothing to it.
let generation = 0;

export const docImageGeneration = () => generation;

export function rememberLocalImage(fileId: string, blob: Blob) {
  uploadedHere.add(fileId);
  localUrls.set(fileId, URL.createObjectURL(blob));
}

export const localImageUrl = (fileId: string) => localUrls.get(fileId);

export const uploadedOnThisPage = (fileId: string) => uploadedHere.has(fileId);

export function releaseDocImageUploads() {
  generation += 1;
  for (const end of Array.from(waiting)) end();
  for (const url of localUrls.values()) URL.revokeObjectURL(url);
  localUrls.clear();
  useDocImageUploadStore.setState({ uploads: [] });
}

/** Keeps the page's screenshot uploads across Doc, Visual and YAML; they go with the workflow or the editor. */
export function useDocImageUploadScope(
  workflowId: string | undefined,
  hostsDoc: boolean,
) {
  useEffect(
    () => (hostsDoc ? releaseDocImageUploads : undefined),
    [workflowId, hostsDoc],
  );
}

// Why a file can't be a screenshot, or null when it can.
export function docImageRefusal(file: File): string | null {
  if (!isDocImageType(file.type))
    return `${file.name || "This file"} isn't a PNG, JPEG, GIF or WebP image.`;
  // Larger files are not decoded at all; up to here they are made smaller when over DOC_IMAGE_MAX_BYTES.
  if (file.size > ATTACHMENT_SIZE_LIMIT_BYTES)
    return `${file.name || "This image"} is over the 30 MiB limit.`;
  return null;
}

const tooLargeMessage = (file: File) =>
  `${file.name || "This image"} is over 4 MiB and couldn't be made small enough. Crop it or save it smaller.`;
const tooManyPixelsMessage = (file: File) =>
  `${file.name || "This image"} is over 80 megapixels. Crop it or save it smaller.`;
const unreadableMessage = (file: File) =>
  `${file.name || "This image"} couldn't be read as an image.`;
const ANIMATED_MESSAGE =
  "This animated image is over 4 MiB and cannot be shrunk without losing the animation; trim it or paste a still.";
const GIF_MESSAGE =
  "This GIF may be animated and is over 4 MiB, so it is not shrunk to a still; trim it or save it as a PNG, JPEG or WebP.";

const tagAt = (bytes: Uint8Array, at: number) =>
  String.fromCharCode(...bytes.slice(at, at + 4));

// A WebP says so in its VP8X chunk's animation flag (RIFF header, flags byte 20, bit 0x02).
async function isAnimatedWebp(file: File): Promise<boolean> {
  const head = new Uint8Array(await file.slice(0, 32).arrayBuffer());
  return (
    tagAt(head, 0) === "RIFF" &&
    tagAt(head, 8) === "WEBP" &&
    tagAt(head, 12) === "VP8X" &&
    (head[20]! & 0x02) !== 0
  );
}

// An animated PNG has an acTL chunk before its first IDAT; each chunk is length, type, data, CRC.
async function isAnimatedPng(file: File): Promise<boolean> {
  let at = 8;
  for (let chunk = 0; chunk < 64 && at + 8 <= file.size; chunk++) {
    const head = new Uint8Array(await file.slice(at, at + 8).arrayBuffer());
    const type = tagAt(head, 4);
    if (type === "acTL") return true;
    if (type === "IDAT") return false;
    at += 12 + new DataView(head.buffer).getUint32(0);
  }
  return false;
}

// Why an image over the limit is not re-encoded, which would keep only its first frame. A GIF is not parsed.
async function animatedRefusal(file: File): Promise<string | null> {
  if (file.type === "image/gif") return GIF_MESSAGE;
  try {
    if (file.type === "image/webp" && (await isAnimatedWebp(file)))
      return ANIMATED_MESSAGE;
    if (file.type === "image/png" && (await isAnimatedPng(file)))
      return ANIMATED_MESSAGE;
  } catch {
    // An unreadable header is left to the decode, which says the file can't be read.
  }
  return null;
}

const u16be = (b: Uint8Array, at: number) => (b[at]! << 8) | b[at + 1]!;
const u24le = (b: Uint8Array, at: number) =>
  b[at]! | (b[at + 1]! << 8) | (b[at + 2]! << 16);

// Width times height as the file's header says, read before a decode; null when the header is not one read here.
async function headerPixels(file: File): Promise<number | null> {
  const head = new Uint8Array(await file.slice(0, 32).arrayBuffer());
  if (file.type === "image/png" && tagAt(head, 12) === "IHDR") {
    const view = new DataView(head.buffer);
    return view.getUint32(16) * view.getUint32(20);
  }
  if (file.type === "image/webp" && tagAt(head, 8) === "WEBP") {
    const chunk = tagAt(head, 12);
    if (chunk === "VP8X") return (u24le(head, 24) + 1) * (u24le(head, 27) + 1);
    if (chunk === "VP8 ")
      return (
        ((head[26]! | (head[27]! << 8)) & 0x3fff) *
        ((head[28]! | (head[29]! << 8)) & 0x3fff)
      );
    if (chunk === "VP8L") {
      const bits = head[21]! | (head[22]! << 8) | (head[23]! << 16);
      const width = (bits & 0x3fff) + 1;
      const height = ((bits >>> 14) | ((head[24]! & 0x0f) << 10)) + 1;
      return width * height;
    }
    return null;
  }
  if (file.type === "image/jpeg" && head[0] === 0xff && head[1] === 0xd8) {
    // Segments follow the start marker; a start-of-frame (C0 to CF, less C4, C8 and CC) holds the size.
    let at = 2;
    for (let segment = 0; segment < 64 && at + 9 <= file.size; segment++) {
      const marker = new Uint8Array(await file.slice(at, at + 9).arrayBuffer());
      if (marker[0] !== 0xff) return null;
      const kind = marker[1]!;
      if (kind >= 0xc0 && kind <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(kind))
        return u16be(marker, 5) * u16be(marker, 7);
      at += 2 + u16be(marker, 2);
    }
  }
  return null;
}

/** The name with an extension that matches `type`: a known image extension is replaced, anything else kept. */
export function docImageName(name: string, type: DocImageType): string {
  const match = /^(.*)\.([^./\\]+)$/.exec(name);
  const ext = match?.[2]?.toLowerCase();
  if (ext && EXTENSIONS[type].includes(ext)) return name;
  const known = ext && Object.values(EXTENSIONS).some((e) => e.includes(ext));
  const stem = (known ? match![1] : name) || "screenshot";
  return `${stem}.${EXTENSIONS[type][0]}`;
}

const asDocImage = (blob: Blob, name: string, type: DocImageType) =>
  blob instanceof File && blob.name === docImageName(name, type)
    ? blob
    : new File([blob], docImageName(name, type), { type });

const SCALES = [1, 0.75, 0.5, 0.35];
// Chromium encodes a canvas side over 16383 px as a cropped WebP, and says nothing.
const MAX_CANVAS_SIDE = 16383;
export const DOC_IMAGE_MAX_PIXELS = 80_000_000;
export const UPLOAD_TIMEOUT_MS = 60_000;

/** The ladder of scales tried for an image of this size; the first keeps the longest side encodable. */
export function docImageScales(width: number, height: number): number[] {
  const fit = Math.min(1, MAX_CANVAS_SIDE / Math.max(width, height, 1));
  return SCALES.map((scale) => scale * fit);
}
const QUALITIES = [0.85, 0.6];

async function encode(
  bitmap: ImageBitmap,
  scale: number,
  quality: number,
): Promise<Blob | null> {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const toBlob = (type: string) =>
    new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, type, quality),
    );
  const webp = await toBlob("image/webp");
  // A browser that can't encode WebP hands back a PNG instead.
  if (webp?.type === "image/webp") return webp;
  // JPEG has no transparency; without a white backing, transparent areas turn black.
  context.globalCompositeOperation = "destination-over";
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  return toBlob("image/jpeg");
}

/**
 * The file to upload: as it is up to DOC_IMAGE_MAX_BYTES, else a still image re-encoded as WebP (or JPEG), smaller
 * until it fits. A string says why there is no file: unreadable, animated or too large.
 */
export async function fitDocImage(file: File): Promise<File | string> {
  if (!isDocImageType(file.type)) return unreadableMessage(file);
  if (file.size <= DOC_IMAGE_MAX_BYTES)
    return asDocImage(file, file.name, file.type);
  const animated = await animatedRefusal(file);
  if (animated) return animated;
  let pixels: number | null = null;
  try {
    pixels = await headerPixels(file);
  } catch {
    // An unreadable header is left to the decode.
  }
  if (pixels !== null && pixels > DOC_IMAGE_MAX_PIXELS)
    return tooManyPixelsMessage(file);
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return unreadableMessage(file);
  }
  try {
    if (bitmap.width * bitmap.height > DOC_IMAGE_MAX_PIXELS)
      return tooManyPixelsMessage(file);
    for (const scale of docImageScales(bitmap.width, bitmap.height))
      for (const quality of QUALITIES) {
        const blob = await encode(bitmap, scale, quality);
        if (
          blob &&
          isDocImageType(blob.type) &&
          blob.size <= DOC_IMAGE_MAX_BYTES
        )
          return asDocImage(blob, file.name, blob.type);
      }
    return tooLargeMessage(file);
  } catch {
    return tooLargeMessage(file);
  } finally {
    bitmap.close();
  }
}

// Files a paste carries. Clipboard text wins, as Office apps put a picture of the copied text next to the text,
// except when the text is only the name of a copied image file, as a file manager's copy gives.
export function pastedFiles(data: DataTransfer | null): File[] {
  if (!data) return [];
  const files = data.files?.length
    ? Array.from(data.files)
    : Array.from(data.items ?? [])
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);
  const text = data.getData("text/plain");
  if (!text) return files;
  const copiedFile =
    files.length > 0 &&
    files.every((file) => isDocImageType(file.type)) &&
    files.some((file) => file.name === text.trim());
  return copiedFile ? files : [];
}

export const hasFiles = (data: DataTransfer | null) =>
  Boolean(data && Array.from(data.types).includes("Files"));

export async function uploadDocImage(
  credentialGetter: CredentialGetter | null,
  file: File,
): Promise<string> {
  const client = await getClient(credentialGetter, "sans-api-v1");
  const formData = new FormData();
  formData.append("file", file);
  const response = await client.post<FormData, { data: { file_id: string } }>(
    "/upload_file",
    formData,
    {
      headers: { "Content-Type": "multipart/form-data" },
      // A hung upload would keep its row saying "Uploading" and its place under the step's cap.
      timeout: UPLOAD_TIMEOUT_MS,
    },
  );
  return response.data.file_id;
}

export type DocImageSignedUrl = {
  file_id: string;
  signed_url: string;
  // Null where the link never expires (the local backend's file:// links).
  expires_at: number | null;
  content_type: string;
};

export async function mintDocImageUrl(
  credentialGetter: CredentialGetter | null,
  fileId: string,
  workflowPermanentId: string,
): Promise<DocImageSignedUrl> {
  const client = await getClient(credentialGetter, "sans-api-v1");
  const response = await client.get<DocImageSignedUrl>(
    `/files/${fileId}/signed-url`,
    { params: { workflow_permanent_id: workflowPermanentId } },
  );
  return response.data;
}
