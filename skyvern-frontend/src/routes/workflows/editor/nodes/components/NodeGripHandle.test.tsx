// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { NodeGripHandle } from "./NodeGripHandle";

/**
 * SKY-9066 — axe / screen-reader pass.
 *
 * axe-core flags three rules on the grip handle's previous markup:
 *   1. `aria-deprecated-attr` — `aria-grabbed` was deprecated in WAI-ARIA 1.1.
 *   2. `aria-allowed-role` — `role="button"` on a native <button> duplicates
 *      the element's intrinsic role.
 *   3. `button-name` / the VoiceOver pass — the generic "Drag to reorder
 *      block" label does not identify which block the handle controls.
 *
 * Rather than adding an axe-core runtime dependency, we assert each rule
 * directly via plain DOM methods. The assertions are tighter than a broad
 * axe sweep (they pinpoint the exact attribute we removed), so a regression
 * shows up as a meaningful test failure instead of an opaque rule-id
 * mismatch. A manual axe-DevTools pass is documented in the PR description.
 */

afterEach(() => {
  cleanup();
});

function getGripButton(): HTMLButtonElement {
  return screen.getByRole("button") as HTMLButtonElement;
}

describe("NodeGripHandle axe compliance (SKY-9066)", () => {
  test("does not emit the deprecated aria-grabbed attribute in any state", () => {
    // aria-grabbed was removed from ARIA 1.1; axe rule `aria-deprecated-attr`
    // flags it. dnd-kit already announces the pickup state via its own
    // `accessibility.announcements.onDragStart`, so we don't need to expose
    // grabbed-ness through ARIA.
    const { rerender } = render(<NodeGripHandle />);
    expect(getGripButton().hasAttribute("aria-grabbed")).toBe(false);
    rerender(<NodeGripHandle isDragging />);
    expect(getGripButton().hasAttribute("aria-grabbed")).toBe(false);
  });

  test("does not duplicate the implicit button role", () => {
    // `role="button"` on a native <button> is redundant (axe
    // `aria-allowed-role`). The native element already carries button
    // semantics.
    render(<NodeGripHandle />);
    expect(getGripButton().hasAttribute("role")).toBe(false);
  });

  test("accessible name identifies the block being reordered", () => {
    // The default label is generic so screen-reader users couldn't tell
    // which of N grip handles they had focused. When NodeHeader passes the
    // block label the accessible name incorporates it.
    render(<NodeGripHandle blockLabel="Login step" />);
    const button = screen.getByRole("button", {
      name: "Drag to reorder block Login step",
    });
    expect(button).toBeTruthy();
  });

  test("falls back to a generic accessible name when no label is supplied", () => {
    // Tests and early mount-time renders may not yet have a label. An empty
    // accessible name would trip axe `button-name`; the generic string
    // keeps the handle named.
    render(<NodeGripHandle />);
    const button = screen.getByRole("button", {
      name: "Drag to reorder block",
    });
    expect(button).toBeTruthy();
  });

  test("announces the pickup keyboard shortcut via aria-keyshortcuts", () => {
    // The keyboard contract is "Space to pick up / drop". Exposing it via
    // `aria-keyshortcuts` lets assistive tech surface the binding without
    // reading the full instructions block every time the handle gets focus.
    render(<NodeGripHandle />);
    expect(getGripButton().getAttribute("aria-keyshortcuts")).toBe("Space");
  });

  test("exposes dragging state through a data attribute, not ARIA", () => {
    // dnd-kit's announcements convey active-drag state. We still need a
    // styling hook, so we use `data-dragging` — an HTML-valid attribute
    // that doesn't collide with any deprecated ARIA rule.
    render(<NodeGripHandle isDragging />);
    const button = getGripButton();
    expect(button.getAttribute("data-dragging")).toBe("true");
    expect(button.hasAttribute("aria-grabbed")).toBe(false);
  });

  test("disabled state exposes aria-disabled and native disabled together", () => {
    // Native `disabled` blocks pointer + keyboard activation; `aria-disabled`
    // is what assistive tech reads. Emitting both keeps the control coherent
    // across input modes.
    render(<NodeGripHandle disabled />);
    const button = getGripButton();
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.disabled).toBe(true);
  });
});

describe("NodeGripHandle hover-only opacity", () => {
  test("renders with opacity-0 + group-hover and focus-visible reveal hooks", () => {
    render(<NodeGripHandle blockLabel="block_1" />);
    const button = getGripButton();
    expect(button.className).toContain("opacity-0");
    expect(button.className).toContain("group-hover:opacity-100");
    expect(button.className).toContain("focus-visible:opacity-100");
    expect(button.className).toContain("data-[dragging=true]:opacity-100");
  });

  test("dragging state stamps data-dragging=true so the opacity rule keeps the grip visible", () => {
    render(<NodeGripHandle blockLabel="block_1" isDragging />);
    const button = getGripButton();
    expect(button.dataset.dragging).toBe("true");
  });
});
