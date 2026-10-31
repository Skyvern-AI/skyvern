import { usePageSlots } from "@/store/PageSlots";

const SEO_CAPABILITIES = [
  "seo_recipe.list",
  "seo_recipe.read",
  "seo_recipe.execute",
  "seo_recipe.update",
];

export function useSeoRecipeAccess():
  | "allowed"
  | "denied"
  | "checking"
  | "unverified" {
  const { permissions } = usePageSlots();
  if (!permissions) return "allowed";
  switch (permissions.status) {
    case "loading":
      return "checking";
    case "ready": {
      const entries = permissions.data.capabilities.filter(({ capability }) =>
        SEO_CAPABILITIES.includes(capability),
      );
      // Older backends do not list SEO capabilities.
      if (entries.length === 0) return "allowed";
      return SEO_CAPABILITIES.every((capability) =>
        entries.some(
          (entry) => entry.capability === capability && entry.allowed,
        ),
      )
        ? "allowed"
        : "denied";
    }
    case "unavailable":
      return permissions.personalWorkspaceOwner || permissions.endpointMissing
        ? "allowed"
        : "unverified";
    case "error":
      return "unverified";
  }
}
