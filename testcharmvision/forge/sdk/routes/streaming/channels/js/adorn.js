/**
 * DOM-adornment: assign stable identifiers to all DOM elements.
 */

(function () {
  console.log("[SYS] adornment evaluated");

  window.__testcharmvision_assignedEls = window.__testcharmvision_assignedEls ?? 0;

  const visited = (window.__testcharmvision_visited =
    window.__testcharmvision_visited ?? new Set());

  function __testcharmvision_generateUniqueId() {
    const timestamp = Date.now().toString(36);
    const randomPart = Math.random().toString(36).substring(2);

    return `sky-${timestamp}-${randomPart}`;
  }

  window.__testcharmvision_generateUniqueId = __testcharmvision_generateUniqueId;

  function __testcharmvision_assignSkyIds(node) {
    if (!node) {
      return;
    }

    if (node.nodeType === 1) {
      if (!node.dataset.skyId) {
        window.__testcharmvision_assignedEls += 1;
        node.dataset.skyId = __testcharmvision_generateUniqueId();
      }

      if (visited.has(node)) {
        return;
      }

      visited.add(node);

      const children = node.querySelectorAll("*");

      children.forEach((child) => {
        __testcharmvision_assignSkyIds(child);
      });
    }
  }

  if (document.body) {
    __testcharmvision_assignSkyIds(document.body);
    console.log(
      "[SYS] adornment: initially assigned skyIds to elements:",
      window.__testcharmvision_assignedEls,
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    __testcharmvision_assignSkyIds(document.body);
    console.log(
      "[SYS] adornment: assigned skyIds to elements on DOMContentLoaded:",
      window.__testcharmvision_assignedEls,
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
          __testcharmvision_assignSkyIds(node);
          console.log(
            "[SYS] adornment: assigned skyIds to new elements:",
            window.__testcharmvision_assignedEls,
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

  window.__testcharmvision_adornment_observer = observer;
})();
