// Keyboard shortcuts skip these so the browser's native per-input undo keeps working.
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  if (target.isContentEditable) return true;
  // Monaco wraps its editor surface in a div with role="textbox"; let
  // it keep native undo as well.
  if (target.getAttribute("role") === "textbox") return true;
  return false;
}
