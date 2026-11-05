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
  test("a reload in the Doc stays in the Doc, and another agent is unaffected", () => {
    store().syncScope("wpid_a", true);
    store().setPresentation("doc");

    useWorkflowPresentationStore.setState({
      workflowPermanentId: null,
      enabled: false,
      presentation: "visual",
    });
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");

    store().syncScope("wpid_b", true);
    expect(store().presentation).toBe("visual");
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
    expect(store().presentation).toBe("visual");
  });

  test("a choice made before the flag resolves is not lost", () => {
    store().syncScope("wpid_a", true);
    store().setPresentation("doc");
    store().syncScope("wpid_a", false);
    expect(store().presentation).toBe("visual");
    store().syncScope("wpid_a", true);
    expect(store().presentation).toBe("doc");
  });

  test("unavailable storage falls back to Visual without throwing", () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(() => store().syncScope("wpid_a", true)).not.toThrow();
      expect(store().presentation).toBe("visual");
    } finally {
      Storage.prototype.getItem = original;
    }
  });
});
