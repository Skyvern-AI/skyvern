import { cn } from "@/util/utils";

/**
 * Classes for the run-hero screenshot. Default view fits to panel width with
 * `margin:auto` in a flex scroll container — centered when shorter than the panel,
 * top-aligned and scrollable when taller. Zoomed = natural size, scrollable on both
 * axes (caller sets the initial top-center scroll).
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
        : "flex overflow-y-auto overflow-x-hidden cursor-zoom-in",
    ),
    image: zoomed ? "mx-auto block max-w-none" : "m-auto w-full",
  };
}
