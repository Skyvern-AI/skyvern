import { useEffect } from "react";

const DND_LIVE_REGION_SELECTOR = '[id^="DndLiveRegion"][aria-live]';

function setPolite(region: Element): void {
  if (region.getAttribute("aria-live") !== "polite") {
    region.setAttribute("aria-live", "polite");
  }
}

function normalizeDndLiveRegions(root: ParentNode): void {
  root
    .querySelectorAll<HTMLElement>(DND_LIVE_REGION_SELECTOR)
    .forEach(setPolite);
}

function normalizeAddedNode(node: Node): void {
  if (!(node instanceof Element)) return;

  if (node.matches(DND_LIVE_REGION_SELECTOR)) {
    setPolite(node);
  }

  normalizeDndLiveRegions(node);
}

function normalizeMutations(records: MutationRecord[]): void {
  records.forEach((record) => {
    if (record.type === "attributes" && record.target instanceof Element) {
      if (record.target.matches(DND_LIVE_REGION_SELECTOR)) {
        setPolite(record.target);
      }
      return;
    }

    record.addedNodes.forEach(normalizeAddedNode);
  });
}

export function PoliteDndLiveRegionPolicy() {
  useEffect(() => {
    const root = document;
    normalizeDndLiveRegions(root);

    const observer = new MutationObserver(normalizeMutations);
    observer.observe(root.body, {
      attributeFilter: ["aria-live"],
      attributes: true,
      childList: true,
      subtree: true,
    });

    return () => observer.disconnect();
  }, []);

  return null;
}
