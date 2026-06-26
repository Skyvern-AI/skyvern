import { describe, expect, it } from "vitest";

import { screenshotZoomClasses } from "./HeroScreenshot.utils";

describe("screenshotZoomClasses", () => {
  it("fits to width and scrolls vertically when not zoomed", () => {
    const { container, image } = screenshotZoomClasses(false);
    expect(container).toContain("cursor-zoom-in");
    expect(container).toContain("overflow-y-auto");
    expect(image).toContain("w-full");
    expect(image).not.toContain("max-w-none");
  });

  it("shows the screenshot at natural size, top-center, when zoomed", () => {
    const { container, image } = screenshotZoomClasses(true);
    expect(container).toContain("cursor-zoom-out");
    expect(container).toContain("overflow-auto");
    expect(image).toContain("mx-auto");
    expect(image).toContain("max-w-none");
  });
});
