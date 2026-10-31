import { availableNodeLibraryItems } from "../panels/nodeLibraryItems";
import type { IntegrationCatalogEntry } from "@/hooks/useIntegrationCatalogQuery";
import { docBlockType, docTagColor } from "./docStepRegistry";

const GROUPS = [
  { color: "blue", name: "Browser" },
  { color: "green", name: "Data" },
  { color: "purple", name: "Logic" },
] as const;

export function docStepChoices({
  agentBlockEnabled = false,
  integrationCatalog,
}: {
  agentBlockEnabled?: boolean;
  integrationCatalog?: readonly IntegrationCatalogEntry[];
} = {}) {
  const items = availableNodeLibraryItems({
    agentBlockEnabled,
    integrationCatalog,
  }).map((item) => {
    const blockType = docBlockType({
      id: "",
      position: { x: 0, y: 0 },
      type: item.nodeType,
      data: {},
    });
    return { ...item, blockType, color: docTagColor(blockType) };
  });
  const known = new Set<string>(GROUPS.map((g) => g.color));
  return [
    ...GROUPS.map((g) => ({
      name: g.name,
      items: items.filter((i) => i.color === g.color),
    })),
    { name: "More", items: items.filter((i) => !known.has(i.color)) },
  ].filter((g) => g.items.length > 0);
}
