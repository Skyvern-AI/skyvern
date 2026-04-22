import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getTextareaCaretCoordinates } from "@/util/caretPosition";
import {
  useAvailableParameters,
  type AvailableParameter,
} from "./useAvailableParameters";

type AnchorPosition = {
  top: number;
  left: number;
};

type AutocompleteState = {
  isOpen: boolean;
  filterText: string;
  /** Character index of the `{{` that opened autocomplete */
  triggerStart: number;
  anchorPosition: AnchorPosition;
  selectedIndex: number;
};

const INITIAL_STATE: AutocompleteState = {
  isOpen: false,
  filterText: "",
  triggerStart: -1,
  anchorPosition: { top: 0, left: 0 },
  selectedIndex: 0,
};

/**
 * Scan backwards from `cursorPos` to find an unmatched `{{`.
 * Returns the index of the first `{` or -1 if not found.
 */
function findTriggerStart(value: string, cursorPos: number): number {
  const before = value.substring(0, cursorPos);
  const lastOpen = before.lastIndexOf("{{");
  if (lastOpen === -1) return -1;

  // Check there's no closing `}}` between the `{{` and cursor
  const between = value.substring(lastOpen + 2, cursorPos);
  if (between.includes("}}")) return -1;

  // Don't trigger if there's a space right after {{
  if (between.length > 0 && between[0] === " ") return -1;

  // Don't trigger if the cursor is inside an already-closed `{{...}}`
  // e.g. `{{current_}}` with cursor before `}}` — the param is already closed
  const after = value.substring(cursorPos);
  const nextClose = after.indexOf("}}");
  const nextOpen = after.indexOf("{{");
  if (nextClose !== -1 && (nextOpen === -1 || nextClose < nextOpen)) {
    return -1;
  }

  return lastOpen;
}

type UseParameterAutocompleteOptions = {
  nodeId: string;
  value: string;
  inputRef: React.RefObject<HTMLTextAreaElement | HTMLInputElement | null>;
  variant: "textarea" | "input";
};

function useParameterAutocomplete({
  nodeId,
  value,
  inputRef,
  variant,
}: UseParameterAutocompleteOptions) {
  const allParameters = useAvailableParameters(nodeId);
  const [state, setState] = useState<AutocompleteState>(INITIAL_STATE);

  // Keep a ref to the latest state for use in event handlers
  const stateRef = useRef(state);
  stateRef.current = state;

  const filteredItems = useMemo(() => {
    if (!state.isOpen) return [];
    const filter = state.filterText.toLowerCase();
    if (!filter) return allParameters;
    const matches = allParameters.filter((p) =>
      p.key.toLowerCase().includes(filter),
    );
    // Prefix matches first so the default selection (index 0) shows ghost text
    matches.sort((a, b) => {
      const aPrefix = a.key.toLowerCase().startsWith(filter) ? 0 : 1;
      const bPrefix = b.key.toLowerCase().startsWith(filter) ? 0 : 1;
      return aPrefix - bPrefix;
    });
    return matches;
  }, [state.isOpen, state.filterText, allParameters]);

  const dismiss = useCallback(() => {
    setState(INITIAL_STATE);
  }, []);

  const computeAnchorPosition = useCallback((): AnchorPosition => {
    const el = inputRef.current;
    if (!el) return { top: 0, left: 0 };

    const rect = el.getBoundingClientRect();

    if (variant === "textarea") {
      const textarea = el as HTMLTextAreaElement;
      const caretCoords = getTextareaCaretCoordinates(
        textarea,
        textarea.selectionStart,
      );
      return {
        top:
          rect.top +
          caretCoords.top +
          parseInt(getComputedStyle(el).lineHeight || "20", 10),
        left: rect.left + caretCoords.left,
      };
    }

    // For single-line inputs, position below the input
    return {
      top: rect.bottom + 2,
      left: rect.left,
    };
  }, [inputRef, variant]);

  /**
   * Called on every value/cursor change to detect or update the `{{` trigger.
   */
  const updateAutocomplete = useCallback(() => {
    const el = inputRef.current;
    if (!el) {
      dismiss();
      return;
    }

    // Only trigger autocomplete when the element is focused — avoid false
    // positives from programmatic value changes (e.g. "+" button, AI improve).
    if (document.activeElement !== el) {
      if (stateRef.current.isOpen) dismiss();
      return;
    }

    const cursorPos = el.selectionStart ?? 0;
    const triggerStart = findTriggerStart(value, cursorPos);

    if (triggerStart === -1) {
      if (stateRef.current.isOpen) {
        dismiss();
      }
      return;
    }

    const filterText = value.substring(triggerStart + 2, cursorPos);

    setState({
      isOpen: true,
      filterText,
      triggerStart,
      anchorPosition: computeAnchorPosition(),
      selectedIndex: 0,
    });
  }, [value, inputRef, dismiss, computeAnchorPosition]);

  // Re-evaluate autocomplete state whenever the value changes
  useEffect(() => {
    // Use a microtask so the DOM has updated cursor position
    const id = requestAnimationFrame(() => {
      updateAutocomplete();
    });
    return () => cancelAnimationFrame(id);
  }, [updateAutocomplete]);

  /**
   * Build the new value after selecting a parameter.
   * Replaces `{{filterText` with `{{selectedKey}}`.
   */
  const buildSelectedValue = useCallback(
    (parameterKey: string): { newValue: string; cursorPos: number } => {
      const { triggerStart } = stateRef.current;
      const el = inputRef.current;
      const cursorPos = el?.selectionStart ?? value.length;
      const replacement = `{{${parameterKey}}}`;
      const before = value.substring(0, triggerStart);
      const after = value.substring(cursorPos);
      const newValue = before + replacement + after;
      return {
        newValue,
        cursorPos: triggerStart + replacement.length,
      };
    },
    [value, inputRef],
  );

  /**
   * Intercept keyboard events when autocomplete is open.
   * Returns true if the event was handled (caller should preventDefault).
   */
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent): boolean => {
      if (!stateRef.current.isOpen || filteredItems.length === 0) return false;

      switch (e.key) {
        case "ArrowDown": {
          e.preventDefault();
          e.stopPropagation();
          setState((prev) => ({
            ...prev,
            selectedIndex:
              prev.selectedIndex < filteredItems.length - 1
                ? prev.selectedIndex + 1
                : 0,
          }));
          return true;
        }
        case "ArrowUp": {
          e.preventDefault();
          e.stopPropagation();
          setState((prev) => ({
            ...prev,
            selectedIndex:
              prev.selectedIndex > 0
                ? prev.selectedIndex - 1
                : filteredItems.length - 1,
          }));
          return true;
        }
        case "Enter":
        case "Tab": {
          e.preventDefault();
          e.stopPropagation();
          return true; // Handled — caller will call selectParameter
        }
        case "Escape": {
          e.preventDefault();
          e.stopPropagation();
          dismiss();
          return true;
        }
        default:
          return false;
      }
    },
    [filteredItems.length, dismiss],
  );

  /**
   * Returns the currently highlighted parameter for Enter/Tab selection,
   * or null if nothing is highlighted.
   */
  const getSelectedParameter = useCallback((): AvailableParameter | null => {
    if (!stateRef.current.isOpen || filteredItems.length === 0) return null;
    return filteredItems[stateRef.current.selectedIndex] ?? null;
  }, [filteredItems]);

  // Ghost text: the untyped remainder of the selected parameter key + closing `}}`
  const ghostText = useMemo(() => {
    if (!state.isOpen || filteredItems.length === 0) return "";
    const selected = filteredItems[state.selectedIndex];
    if (!selected) return "";
    const filter = state.filterText.toLowerCase();
    // Only show ghost text when the selected item is a prefix match
    if (selected.key.toLowerCase().startsWith(filter)) {
      return selected.key.substring(state.filterText.length) + "}}";
    }
    return "";
  }, [state.isOpen, state.filterText, state.selectedIndex, filteredItems]);

  // Text before cursor — used to position the ghost text overlay
  const textBeforeCursor = useMemo(() => {
    if (!state.isOpen) return "";
    const cursorPos = state.triggerStart + 2 + state.filterText.length;
    return value.substring(0, cursorPos);
  }, [state.isOpen, state.triggerStart, state.filterText, value]);

  return {
    isOpen: state.isOpen,
    filterText: state.filterText,
    anchorPosition: state.anchorPosition,
    selectedIndex: state.selectedIndex,
    filteredItems,
    ghostText,
    textBeforeCursor,
    dismiss,
    handleKeyDown,
    buildSelectedValue,
    getSelectedParameter,
    updateAutocomplete,
  };
}

export { useParameterAutocomplete };
