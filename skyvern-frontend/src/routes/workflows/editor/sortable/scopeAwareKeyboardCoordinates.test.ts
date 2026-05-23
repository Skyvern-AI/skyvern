// @vitest-environment jsdom

import { describe, expect, test, vi } from "vitest";
import { KeyboardCode } from "@dnd-kit/core";

import { createScopeAwareKeyboardCoordinates } from "./scopeAwareKeyboardCoordinates";

type Rect = {
  top: number;
  left: number;
  width: number;
  height: number;
  bottom: number;
  right: number;
};

function makeRect(top: number, left = 0, height = 40, width = 200): Rect {
  return {
    top,
    left,
    width,
    height,
    bottom: top + height,
    right: left + width,
  };
}

type FakeContainer = {
  id: string;
  key: string;
  rect: { current: Rect };
  node: { current: HTMLElement };
  disabled: boolean;
  data: {
    current: {
      sortable: { containerId: string; items: string[]; index: number };
    };
  };
};

function makeContainer(
  id: string,
  rect: Rect,
  index: number,
  containerId: string = "scope:top-level",
): FakeContainer {
  return {
    id,
    key: id,
    rect: { current: rect },
    node: {
      current: {
        getBoundingClientRect: () => rect,
      } as unknown as HTMLElement,
    },
    disabled: false,
    data: {
      current: {
        sortable: { containerId, items: [], index },
      },
    },
  };
}

function buildArgs({
  activeId,
  activeRect,
  containers,
}: {
  activeId: string;
  activeRect: Rect;
  containers: FakeContainer[];
}) {
  const droppableRects = new Map<string, Rect>();
  for (const container of containers) {
    droppableRects.set(container.id, container.rect.current);
  }
  const droppableContainersMap = new Map(containers.map((c) => [c.id, c]));
  const droppableContainers = {
    getEnabled: () => containers.filter((c) => !c.disabled),
    get: (id: string) => droppableContainersMap.get(id),
    getNodeFor: (id: string) => droppableContainersMap.get(id)?.node.current,
    toArray: () => containers,
  };
  const active = containers.find((c) => c.id === activeId);
  return {
    active: activeId,
    currentCoordinates: { x: 0, y: activeRect.top },
    context: {
      active,
      collisionRect: activeRect,
      droppableRects,
      droppableContainers,
      over: null,
      scrollableAncestors: [],
    },
  };
}

function makeDownEvent(): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { code: KeyboardCode.Down });
  Object.defineProperty(event, "code", { value: KeyboardCode.Down });
  return event;
}

describe("createScopeAwareKeyboardCoordinates (SKY-9051)", () => {
  test("scopes Down navigation to the active block's scope siblings", () => {
    // Layout: active is loop-a (y=120). loop-b (y=240) is the in-scope
    // sibling below; top-far (y=200) is a top-level sibling that is
    // geometrically closer below. Without scope filtering, Down would step
    // to top-far; with scope filtering, it must step to loop-b.
    const containers = [
      makeContainer("top-1", makeRect(0), 0),
      makeContainer("top-far", makeRect(200), 1),
      makeContainer("loop-a", makeRect(120), 0, "scope:loop-1:__main__"),
      makeContainer("loop-b", makeRect(240), 1, "scope:loop-1:__main__"),
    ];
    const scopeKeyForId = (id: string): string =>
      id.startsWith("loop-") ? "scope:loop-1:__main__" : "scope:top-level";

    const wrapped = createScopeAwareKeyboardCoordinates(scopeKeyForId);
    const args = buildArgs({
      activeId: "loop-a",
      activeRect: makeRect(120),
      containers,
    });

    const result = wrapped(makeDownEvent(), args as never);

    const loopBTop = containers.find((c) => c.id === "loop-b")?.rect.current
      .top;
    const topFarTop = containers.find((c) => c.id === "top-far")?.rect.current
      .top;
    expect(result).toBeDefined();
    expect(result?.y).toBe(loopBTop);
    expect(result?.y).not.toBe(topFarTop);
  });

  test("falls through to upstream behaviour when no active draggable", () => {
    const scopeKeyForId = vi.fn(() => "scope:top-level");
    const wrapped = createScopeAwareKeyboardCoordinates(scopeKeyForId);

    const result = wrapped(makeDownEvent(), {
      active: "",
      currentCoordinates: { x: 0, y: 0 },
      context: {
        active: null,
        collisionRect: null,
        droppableRects: new Map(),
        droppableContainers: {
          getEnabled: () => [],
          get: () => undefined,
          getNodeFor: () => undefined,
          toArray: () => [],
        },
        over: null,
        scrollableAncestors: [],
      },
    } as never);

    expect(result).toBeUndefined();
    // The scope resolver should never be consulted without an active block,
    // so guarding the active=null branch keeps it cheap.
    expect(scopeKeyForId).not.toHaveBeenCalled();
  });

  test("preserves underlying map for off-scope lookups via .get()", () => {
    // sortableKeyboardCoordinates calls droppableContainers.get(active.id)
    // and .get(closestId). Scope filtering only narrows the candidate list
    // surfaced by getEnabled; the underlying map must still resolve active +
    // target so coordinate math works.
    const containers = [
      makeContainer("top-1", makeRect(0), 0),
      makeContainer("loop-a", makeRect(120), 0, "scope:loop-1:__main__"),
      makeContainer("loop-b", makeRect(180), 1, "scope:loop-1:__main__"),
    ];
    const scopeKeyForId = (id: string): string =>
      id.startsWith("loop-") ? "scope:loop-1:__main__" : "scope:top-level";

    const wrapped = createScopeAwareKeyboardCoordinates(scopeKeyForId);
    const args = buildArgs({
      activeId: "loop-a",
      activeRect: makeRect(120),
      containers,
    });

    const result = wrapped(makeDownEvent(), args as never);

    expect(result).toBeDefined();
    // The resolved coordinates target the in-scope sibling loop-b.
    expect(result?.y).toBe(180);
  });
});
