import { cn } from "@/util/utils";

/**
 * Classes for the run-hero screenshot. The default view fits the screenshot to
 * the panel width and scrolls vertically, so long full-page captures aren't
 * cropped (object-contain shrank tall captures to an unreadable sliver and the
 * overflow was clipped). Click toggles to natural size (`zoomed`), scrollable on
 * both axes; the caller sets the initial scroll to top-center.
 */
export function screenshotZoomClasses(zoomed: boolean): {
  container: string;
  image: string;
} {
  return {
    container: cn(
      "absolute inset-0",
      zoomed
        ? "overflow-auto cursor-zoom-out bg-slate-950"
        : "overflow-y-auto overflow-x-hidden cursor-zoom-in",
    ),
    image: zoomed ? "mx-auto block max-w-none" : "block w-full",
  };
}
