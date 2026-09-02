import { useEffect } from "react";
import { useLocation } from "react-router-dom";

function useScrollToHash(ready = true) {
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash || !ready) return;
    document.getElementById(hash.slice(1))?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }, [hash, ready]);
}

export { useScrollToHash };
