/**
 * DOM-adornment: assign stable identifiers to all DOM elements.
 */

(function () {
  console.log("[SYS] adornment evaluated");

  window.__skyvern_assignedEls = window.__skyvern_assignedEls ?? 0;

  const visited = (window.__skyvern_visited =
    window.__skyvern_visited ?? new Set());

  function __skyvern_generateUniqueId() {
    const timestamp = Date.now().toString(36);
    const randomPart = Math.random().toString(36).substring(2);

    return `sky-${timestamp}-${randomPart}`;
  }

  window.__skyvern_generateUniqueId = __skyvern_generateUniqueId;

  function __skyvern_assignSkyIds(node) {
    if (!node) {
      return;
    }

    if (node.nodeType === 1) {
      if (!node.dataset.skyId) {
        window.__skyvern_assignedEls += 1;
        node.dataset.skyId = __skyvern_generateUniqueId();
      }

      if (visited.has(node)) {
        return;
      }

      visited.add(node);

      const children = node.querySelectorAll("*");

      children.forEach((child) => {
        __skyvern_assignSkyIds(child);
      });
    }
  }

  if (document.body) {
    __skyvern_assignSkyIds(document.body);
    console.log(
      "[SYS] adornment: initially assigned skyIds to elements:",
      window.__skyvern_assignedEls,
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    __skyvern_assignSkyIds(document.body);
    console.log(
      "[SYS] adornment: assigned skyIds to elements on DOMContentLoaded:",
      window.__skyvern_assignedEls,
    );
  });

  const observerConfig = {
    childList: true,
    subtree: true,
  };

  const observer = new MutationObserver(function (mutationsList) {
    for (const mutation of mutationsList) {
      if (mutation.type === "childList") {
        mutation.addedNodes.forEach((node) => {
          __skyvern_assignSkyIds(node);
          console.log(
            "[SYS] adornment: assigned skyIds to new elements:",
            window.__skyvern_assignedEls,
          );
        });
      }
    }
  });

  function observeWhenReady() {
    if (document.body) {
      observer.observe(document.body, observerConfig);
    } else {
      document.addEventListener("DOMContentLoaded", () => {
        if (document.body) {
          observer.observe(document.body, observerConfig);
        }
      });
    }
  }

  observeWhenReady();

  window.__skyvern_adornment_observer = observer;
})();
