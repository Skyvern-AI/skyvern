type PendingSop = { workflowPermanentId: string; file: File };

// Home hands the chosen PDF to the editor in memory: a File fits neither the URL nor
// sessionStorage, and the editor consumes it exactly once, like `?record=1`.
let pendingSop: PendingSop | null = null;

export function rememberSopHandoffFile(
  workflowPermanentId: string,
  file: File,
): void {
  pendingSop = { workflowPermanentId, file };
}

export function takeSopHandoffFile(workflowPermanentId: string): File | null {
  if (!pendingSop || pendingSop.workflowPermanentId !== workflowPermanentId) {
    return null;
  }
  const { file } = pendingSop;
  pendingSop = null;
  return file;
}
