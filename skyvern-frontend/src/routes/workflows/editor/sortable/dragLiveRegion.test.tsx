import { DndContext } from "@dnd-kit/core";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { buildDragAnnouncements } from "./dragAnnouncements";
import { PoliteDndLiveRegionPolicy } from "./dragLiveRegionPolicy";

describe("drag live region", () => {
  test("renders a polite aria-live container, never assertive", async () => {
    const announcements = buildDragAnnouncements([
      { id: "a", data: { label: "Step A" } },
    ]);

    render(
      <DndContext
        accessibility={{
          announcements,
          screenReaderInstructions: {
            draggable: "Press space to pick up.",
          },
        }}
      >
        <PoliteDndLiveRegionPolicy />
        <div />
      </DndContext>,
    );

    // dnd-kit renders one or more aria-live regions after mount. Find them all.
    await waitFor(() => {
      expect(
        document.querySelectorAll('[id^="DndLiveRegion"][aria-live]').length,
      ).toBeGreaterThan(0);
    });

    const liveRegions = document.querySelectorAll(
      '[id^="DndLiveRegion"][aria-live]',
    );
    liveRegions.forEach((region) => {
      expect(region.getAttribute("aria-live")).toBe("polite");
    });
  });
});
