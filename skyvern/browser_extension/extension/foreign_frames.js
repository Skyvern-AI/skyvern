(() => {
  const ownOrigin = chrome.runtime.getURL("");
  const MAX_ELEMENTS = 5_000;
  const MAX_FRAME_DEPTH = 5;
  const MAX_OBSERVED_ROOTS = 200;
  const MAX_EMPTY_ELEMENTS = 200;
  const IGNORABLE_ELEMENTS = new Set([
    "style",
    "link",
    "script",
    "template",
    "meta",
    "slot",
  ]);
  const OBSERVER_OPTIONS = {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["src"],
  };
  const emptiedHosts = new WeakSet();
  let epoch = null;
  let revision = -1;
  let visible = null;
  let observer = null;
  let observedRoots = new Map();

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return false;
    if (message?.type === "skyvern.indicator.state") {
      applyState(message);
      return false;
    }
    if (
      message?.type !== "skyvern.foreignFrames.remove" ||
      !Number.isFinite(message.expiresAt) ||
      Date.now() > message.expiresAt
    )
      return false;

    if (visible === false) {
      sendResponse({ removed: 0 });
      return false;
    }
    const removed = observer
      ? scan(
          [{ element: document.documentElement, depth: 0 }],
          message.expiresAt,
        )
      : arm(message.expiresAt);
    sendResponse({ removed });
    return false;
  });

  function applyState(state) {
    if (
      typeof state?.visible !== "boolean" ||
      typeof state.epoch !== "string" ||
      !Number.isSafeInteger(state.revision) ||
      (state.epoch === epoch && state.revision <= revision)
    )
      return;
    epoch = state.epoch;
    revision = state.revision;
    visible = state.visible;
    if (state.visible) {
      if (!observer) arm();
    } else {
      observer?.disconnect();
      observer = null;
      observedRoots = new Map();
    }
  }

  function observeRoot(root, depth) {
    if (observedRoots.has(root)) return;
    if (observedRoots.size >= MAX_OBSERVED_ROOTS) {
      for (const observed of observedRoots.keys()) {
        if (observed.host ? !observed.host.isConnected : !observed.defaultView)
          observedRoots.delete(observed);
      }
      observer?.disconnect();
      for (const observed of observedRoots.keys())
        observer?.observe(observed, OBSERVER_OPTIONS);
      if (observedRoots.size >= MAX_OBSERVED_ROOTS) return;
    }
    observedRoots.set(root, depth);
    observer?.observe(root, OBSERVER_OPTIONS);
  }

  function arm(expiresAt = Infinity) {
    observeRoot(document, 0);
    const removed = scan(
      [{ element: document.documentElement, depth: 0 }],
      expiresAt,
    );
    observer = new MutationObserver((records) => {
      const pending = [];
      for (const record of records) {
        const depth =
          observedRoots.get(record.target.getRootNode()) ??
          observedRoots.get(record.target.ownerDocument);
        if (depth === undefined) continue;
        if (record.type === "childList") {
          for (const element of record.addedNodes) {
            if (element.nodeType === Node.ELEMENT_NODE)
              pending.push({ element, depth });
          }
        } else if (["iframe", "frame"].includes(record.target.localName)) {
          pending.push({ element: record.target, depth });
        }
      }
      scan(pending);
    });
    for (const root of observedRoots.keys())
      observer.observe(root, OBSERVER_OPTIONS);
    return removed;
  }

  function isForeignFrame(element) {
    if (!["iframe", "frame"].includes(element.localName)) return false;
    const attribute = element.getAttribute("src") || "";
    const src = attribute.startsWith("chrome-extension://")
      ? attribute
      : element.src || "";
    return src.startsWith("chrome-extension://") && !src.startsWith(ownOrigin);
  }

  function isEmptyHost(host) {
    if (
      host === host.ownerDocument.documentElement ||
      host === host.ownerDocument.body
    )
      return false;
    const pending = [host];
    let visited = 0;
    while (pending.length) {
      const node = pending.pop();
      if (node !== host && node.nextSibling) pending.push(node.nextSibling);
      if (node.nodeType === Node.TEXT_NODE && node.textContent.trim())
        return false;
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (++visited > MAX_EMPTY_ELEMENTS) return false;
      if (node !== host) {
        if (IGNORABLE_ELEMENTS.has(node.localName) || isForeignFrame(node))
          continue;
        if (
          !["div", "span"].includes(node.localName) &&
          !node.localName.includes("-")
        )
          return false;
      }
      let shadow;
      try {
        shadow = chrome.dom.openOrClosedShadowRoot(node);
      } catch {
        return false;
      }
      if (shadow?.firstChild) pending.push(shadow.firstChild);
      if (node.firstChild) pending.push(node.firstChild);
    }
    return true;
  }

  function scan(pending, expiresAt = Infinity) {
    let visited = 0;
    let removed = 0;
    const seen = new Set();
    while (pending.length && visited < MAX_ELEMENTS) {
      if (Date.now() > expiresAt) break;
      const { element, depth, siblings } = pending.pop();
      if (!element) continue;
      if (siblings && element.nextElementSibling) {
        pending.push({
          element: element.nextElementSibling,
          depth,
          siblings: true,
        });
      }
      if (seen.has(element)) continue;
      seen.add(element);
      visited += 1;
      if (emptiedHosts.has(element) && isEmptyHost(element)) {
        element.remove();
        continue;
      }
      const isFrame = ["iframe", "frame"].includes(element.localName);
      if (isFrame) {
        if (isForeignFrame(element)) {
          let root = element.getRootNode();
          let host = null;
          element.remove();
          while (root.host) {
            if (!isEmptyHost(root.host)) break;
            host = root.host;
            root = host.getRootNode();
          }
          // An added host may already be detached by the focus loss before this batch runs.
          if (host) {
            emptiedHosts.add(host);
            host.remove();
          }
          removed += 1;
          continue;
        }
        if (depth < MAX_FRAME_DEPTH) {
          try {
            const child = element.contentDocument;
            if (child) {
              observeRoot(child, depth + 1);
              pending.push({
                element: child.documentElement,
                depth: depth + 1,
              });
            }
          } catch {}
        }
      }
      let shadow;
      try {
        shadow = chrome.dom.openOrClosedShadowRoot(element);
      } catch {}
      if (shadow) {
        observeRoot(shadow, depth);
        pending.push({
          element: shadow.firstElementChild,
          depth,
          siblings: true,
        });
      }
      if (element.firstElementChild) {
        pending.push({
          element: element.firstElementChild,
          depth,
          siblings: true,
        });
      }
    }
    return removed;
  }

  function query() {
    try {
      void chrome.runtime
        .sendMessage({ type: "skyvern.indicator.query" })
        .then(applyState)
        .catch(() => undefined);
    } catch {}
  }

  query();
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) query();
  });
})();
