import { useLayoutEffect, useState, type CSSProperties } from "react";

// Height for a full-bleed page that fills the viewport below whatever app chrome
// (banners, shell padding) sits above it, so the page never scrolls.
export function useFillViewportBelow<T extends HTMLElement>(): {
  ref: (el: T | null) => void;
  style: CSSProperties;
} {
  const [el, setEl] = useState<T | null>(null);
  const [top, setTop] = useState(0);
  useLayoutEffect(() => {
    if (!el) return;
    const measure = () =>
      setTop(
        Math.max(
          0,
          Math.round(el.getBoundingClientRect().top + window.scrollY),
        ),
      );
    measure();
    window.addEventListener("resize", measure);
    // Chrome above the page (a banner loading, growing or closing) resizes a
    // sibling before this page or one of its ancestors; the body is often
    // fixed-height, so watching it alone misses those changes.
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(measure);
    for (
      let node: Element | null = el;
      node && node !== document.body;
      node = node.parentElement
    ) {
      for (
        let sibling = node.previousElementSibling;
        sibling;
        sibling = sibling.previousElementSibling
      ) {
        observer?.observe(sibling);
      }
    }
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [el]);
  // Browsers without dvh drop this declaration and keep the h-screen class.
  return { ref: setEl, style: { height: `calc(100dvh - ${top}px)` } };
}
