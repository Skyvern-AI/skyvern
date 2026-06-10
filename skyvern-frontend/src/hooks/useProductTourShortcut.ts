import { useEffect } from "react";
import { useProductTourStore } from "@/store/ProductTourStore";

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement
  ) {
    return true;
  }
  if (target instanceof HTMLSelectElement) return true;
  return target.isContentEditable;
}

function useProductTourShortcut() {
  const requestTour = useProductTourStore((s) => s.requestTour);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (
        e.key === "?" &&
        e.shiftKey &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        if (isEditableTarget(e.target)) return;
        e.preventDefault();
        requestTour();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [requestTour]);
}

export { useProductTourShortcut };
