(() => {
  let visible = false;
  let epoch = null;
  let revision = -1;
  let host = null;
  const suppressions = new Map();

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return false;
    switch (message?.type) {
      case "skyvern.indicator.state":
        applyState(message);
        return false;
      case "skyvern.indicator.suppress":
        if (typeof message.token !== "string" || !message.token) return false;
        suppress(message.token);
        render();
        if (document.visibilityState === "hidden") {
          sendResponse({ ok: true });
          return false;
        }
        acknowledgeHidden(sendResponse);
        return true;
      case "skyvern.indicator.release":
        release(message.token);
        sendResponse({ ok: true });
        return false;
      default:
        return false;
    }
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
    render();
  }

  function suppress(token) {
    if (typeof token !== "string" || !token || suppressions.has(token)) return;
    suppressions.set(
      token,
      setTimeout(() => release(token), 60_000),
    );
  }

  function release(token) {
    clearTimeout(suppressions.get(token));
    suppressions.delete(token);
    render();
  }

  function acknowledgeHidden(sendResponse) {
    let done = false;
    let frame;
    const finish = () => {
      if (done) return;
      done = true;
      cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", onVisibility);
      sendResponse({ ok: true });
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") finish();
    };
    document.addEventListener("visibilitychange", onVisibility);
    frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(finish);
    });
  }

  function makeHost() {
    const element = document.createElement("skyvern-control-indicator");
    element.setAttribute("aria-hidden", "true");
    for (const [name, value] of Object.entries({
      all: "initial",
      position: "fixed",
      inset: "0",
      "z-index": "2147483647",
      "pointer-events": "none",
    }))
      element.style.setProperty(name, value, "important");
    const shadow = element.attachShadow({ mode: "closed" });
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(`
:host{all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;display:block;color-scheme:normal}
*,*::before,*::after{box-sizing:border-box;pointer-events:none}
.frame{position:fixed;inset:0;pointer-events:none;overflow:hidden;border-radius:10px}
.edge,.glow,.rim{position:absolute;inset:0;overflow:hidden;border-radius:inherit}
.edge{padding:2px;mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);mask-composite:exclude}
.glow{opacity:.55;mask:linear-gradient(#fff,transparent) top/100% 20px,linear-gradient(0deg,#fff,transparent) bottom/100% 20px,linear-gradient(90deg,#fff,transparent) left/20px 100%,linear-gradient(270deg,#fff,transparent) right/20px 100%;mask-repeat:no-repeat;animation:breathe 4s ease-in-out infinite}
.sweep{position:absolute;left:50%;top:50%;width:150vmax;height:150vmax;background:conic-gradient(from 25deg,#6D6CF6,#D9C8FF 28%,#022D46 55%,#6D6CF6 80%,#6D6CF6);transform:translate(-50%,-50%) rotate(0turn);animation:travel 8s linear infinite;will-change:transform}
.pill{position:fixed;top:12px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:9px;height:32px;padding:0 13px 0 8px;border-radius:999px;white-space:nowrap;font:500 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:.05px;color:#fff;background:#16152ee0;backdrop-filter:blur(12px);box-shadow:0 4px 16px #16152e30,0 0 18px #6d6cf622}
.rim{padding:1px;mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);mask-composite:exclude}
.rim .sweep{width:300px;height:300px;background:conic-gradient(#6D6CF6,#D9C8FF 20%,#6D6CF6 40%,#022D46 70%,#6D6CF6);animation-duration:12s;opacity:.85}
.mark{width:20px;height:20px;border-radius:50%;display:block;flex:none}
.dot{position:relative;width:6px;height:6px;border-radius:50%;background:#9be6ca;box-shadow:0 0 6px #9be6ca55;flex:none;margin-left:3px}
.dot::after{content:"";position:absolute;inset:-3px;border:1px solid #9be6ca;border-radius:50%;opacity:.35;animation:pulse 4s ease-in-out infinite}
@media(max-width:599px){.pill{top:auto;bottom:12px}}
@keyframes travel{to{transform:translate(-50%,-50%) rotate(1turn)}}
@keyframes breathe{0%,100%{opacity:.55}50%{opacity:.9}}
@keyframes pulse{0%,100%{transform:scale(.8);opacity:.15}50%{transform:scale(1.25);opacity:.45}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;will-change:auto!important}.dot::after{opacity:0}}

.label::after{content:"Skyvern is controlling this tab"}
`);
    shadow.adoptedStyleSheets = [sheet];
    const add = (parent, tag, className) => {
      const child = document.createElement(tag);
      child.className = className;
      parent.append(child);
      return child;
    };
    const frame = add(shadow, "div", "frame");
    add(add(frame, "div", "glow"), "div", "sweep");
    add(add(frame, "div", "edge"), "div", "sweep");
    const pill = add(shadow, "div", "pill");
    add(add(pill, "span", "rim"), "span", "sweep");
    const mark = add(pill, "img", "mark");
    mark.src =
      "data:image/png;base64,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";
    mark.alt = "";
    add(pill, "span", "label");
    add(pill, "span", "dot");
    return element;
  }

  function render() {
    const root = document.documentElement;
    if (!root) return;
    if (!host) {
      if (!visible) return;
      host = makeHost();
    }
    host.style.setProperty(
      "display",
      visible && suppressions.size === 0 ? "block" : "none",
      "important",
    );
    if (host.parentNode !== root) {
      for (const other of document.querySelectorAll(
        "skyvern-control-indicator",
      )) {
        if (other !== host) other.remove();
      }
      root.append(host);
    }
  }

  function query() {
    try {
      void chrome.runtime
        .sendMessage({ type: "skyvern.indicator.query" })
        .then((state) => {
          for (const token of state?.captureTokens ?? []) suppress(token);
          applyState(state);
          render();
        })
        .catch(() => undefined);
    } catch {}
  }

  query();
  new MutationObserver(() => {
    if (!host || host.parentNode !== document.documentElement) render();
  }).observe(document, { childList: true, subtree: true });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) query();
  });
})();
