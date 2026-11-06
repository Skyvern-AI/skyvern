import { availableNodeLibraryItems } from "../panels/nodeLibraryItems";
import type { DocNestKind, NodeType } from "./docEditApi";

let message = "";
const listeners = new Set<() => void>();

export function subscribeDocAnnouncement(onChange: () => void) {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

export const docAnnouncement = () => message;

export function clearDocAnnouncement() {
  message = "";
}

/** Read out once by screen readers, without moving focus. */
export function announceDoc(text: string) {
  // A repeat of the same words must be new text in the DOM to be read again; a trailing no-break space does that.
  message = message === text ? text + String.fromCharCode(0xa0) : text;
  listeners.forEach((listener) => listener());
}

export function announceNest(kind: DocNestKind, outdent: boolean) {
  announceDoc(`Moved ${outdent ? "out of" : "into"} the ${kind}`);
}

// Named as the step picker names it.
const typeTitle = (type: NodeType) =>
  (
    availableNodeLibraryItems().find((item) => item.nodeType === type)?.title ??
    type
  ).replace(/ Block$/, "");

export const announceAdded = (type: NodeType) =>
  announceDoc(`Added ${typeTitle(type)} step`);
export const announceTurnedInto = (type: NodeType) =>
  announceDoc(`Turned into ${typeTitle(type)}`);
export const announceMoved = (to: "up" | "down" | { place: string }) =>
  announceDoc(typeof to === "string" ? `Moved ${to}` : `Moved to ${to.place}`);
