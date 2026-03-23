import { useCallback, useEffect, useState } from "react";

function useShimmerText<T extends HTMLElement = HTMLElement>(active: boolean) {
  const [element, setElement] = useState<T | null>(null);

  const ref = useCallback((el: T | null) => {
    setElement(el);
  }, []);

  useEffect(() => {
    if (!active || !element) return;

    // Apply static styles for background-clip: text
    element.style.background =
      "linear-gradient(90deg, rgba(255,255,255,0.4) 25%, rgba(255,255,255,1) 50%, rgba(255,255,255,0.4) 75%)";
    element.style.backgroundSize = "200% 100%";
    element.style.webkitBackgroundClip = "text";
    element.style.backgroundClip = "text";
    element.style.webkitTextFillColor = "transparent";

    const animation = element.animate(
      [
        { backgroundPosition: "200% center" },
        { backgroundPosition: "-200% center" },
      ],
      { duration: 2000, iterations: Infinity, easing: "linear" },
    );

    return () => {
      animation.cancel();
      element.style.background = "";
      element.style.backgroundSize = "";
      element.style.backgroundPosition = "";
      element.style.webkitBackgroundClip = "";
      element.style.backgroundClip = "";
      element.style.webkitTextFillColor = "";
    };
  }, [active, element]);

  return ref;
}

export { useShimmerText };
