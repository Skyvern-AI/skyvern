let installed = false;

export function installTranslationCrashGuard(): void {
  if (installed) return;
  if (typeof Node === "undefined" || !Node.prototype) return;
  installed = true;

  const originalRemoveChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function <T extends Node>(
    this: Node,
    child: T,
  ): T {
    // Translation extensions reparent text nodes, so React's stale reference
    // would throw NotFoundError here; no-op when the node is not our child.
    if (child.parentNode !== this) {
      return child;
    }
    return originalRemoveChild.call(this, child) as T;
  };

  const originalInsertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function <T extends Node>(
    this: Node,
    node: T,
    referenceNode: Node | null,
  ): T {
    // The reference node was moved out by a translation extension; append
    // instead of throwing NotFoundError so the node still mounts.
    if (referenceNode && referenceNode.parentNode !== this) {
      return originalInsertBefore.call(this, node, null) as T;
    }
    return originalInsertBefore.call(this, node, referenceNode) as T;
  };
}
