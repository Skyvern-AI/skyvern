export function adminOnlyReason(action: string): string {
  return `Only organization admins can ${action}. Ask an admin to do this.`;
}
