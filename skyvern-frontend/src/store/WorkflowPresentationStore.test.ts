// @vitest-environment jsdom
import { afterEach, describe, expect, test } from "vitest";

import {
  openNewAgentInDoc,
  useWorkflowPresentationStore,
} from "./WorkflowPresentationStore";

const store = () => useWorkflowPresentationStore.getState();

afterEach(() => {
  localStorage.clear();
  useWorkflowPresentationStore.setState({
    presentation: "visual",
    workflowPermanentId: null,
    enabled: false,
  });
});

describe("remembering Doc or Visual per agent", () => {
  test("with nothing stored an agent opens in the Doc", () => {
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");
    expect(localStorage.length).toBe(0);
  });

  test("a stored Visual reopens in Visual, a stored Doc in the Doc, and another agent is unaffected", () => {
    store().syncScope("wpid_a", true);
    store().setPresentation("visual");
    store().syncScope("wpid_b", true);
    store().setPresentation("doc");

    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("visual");
    store().syncScope("wpid_b", true);
    expect(store().presentation).toBe("doc");
    store().syncScope("wpid_c", true);
    expect(store().presentation).toBe("doc");
  });

  test("restoreView gives back the Doc with nothing stored, a stored Visual, and Visual with the Doc off", () => {
    store().syncScope("wpid_a", true);
    store().showVisualUnremembered();
    store().restoreView();
    expect(store().presentation).toBe("doc");

    store().setPresentation("visual");
    store().showVisualUnremembered();
    store().restoreView();
    expect(store().presentation).toBe("visual");

    store().syncScope("wpid_b", false);
    store().restoreView();
    expect(store().presentation).toBe("visual");
  });

  test("a Visual shown without a click is not remembered", () => {
    store().syncScope("wpid_a", true);
    store().showVisualUnremembered();
    expect(store().presentation).toBe("visual");
    expect(localStorage.length).toBe(0);
    store().syncScope("wpid_b", true);
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");
  });

  test("an agent created from a demonstration opens in the Doc, but a stored Visual choice wins", () => {
    openNewAgentInDoc("wpid_new");
    store().syncScope("wpid_new", true);
    expect(store().presentation).toBe("doc");

    store().setPresentation("visual");
    store().syncScope("wpid_other", true);
    store().syncScope("wpid_new", true);
    expect(store().presentation).toBe("visual");
  });

  test("with the Doc flag off it stays Visual and remembers nothing", () => {
    store().syncScope("wpid_a", false);
    store().setPresentation("doc");
    expect(store().presentation).toBe("visual");
    expect(localStorage.length).toBe(0);
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");
  });

  test("a choice made before the flag resolves is not lost", () => {
    store().syncScope("wpid_a", true);
    store().setPresentation("doc");
    store().syncScope("wpid_a", false);
    expect(store().presentation).toBe("visual");
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");
  });

  test("unavailable storage opens the Doc without throwing", () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(() => store().syncScope("wpid_a", true)).not.toThrow();
      expect(store().presentation).toBe("doc");
    } finally {
      Storage.prototype.getItem = original;
    }
  });
});
