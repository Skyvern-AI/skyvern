// Cursor overlay for Playwright video recordings.
// Renders a visible cursor dot, movement trail, and click ring.
// All elements use data-pw-overlay so they can be hidden during screenshots
// and excluded from DOM scraping (see domUtils.js processElement).
//
// No product-specific naming — only generic __pw-* identifiers.

// --- Initialization ---
// Called once per page. Guards against double-init and missing DOM.
function __pwCursorInit() {
  if (window.__PW_CURSOR_VIS__) return;
  if (!document.head || !document.body) return;

  const style = document.createElement("style");
  style.textContent = `
    #__pw-cursor {
      position: fixed;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: rgba(255, 68, 68, 0.7);
      border: 2px solid rgba(255, 255, 255, 0.9);
      pointer-events: none;
      z-index: 2147483647;
      transform: translate(-50%, -50%);
      box-shadow: 0 0 4px rgba(0,0,0,0.3);
      top: 0; left: 0;
    }
    @keyframes __pw-trail-fade {
      0%   { opacity: 0.5; }
      100% { opacity: 0; }
    }
    @keyframes __pw-click-ring {
      0%   { transform: translate(-50%, -50%) scale(0.5); opacity: 1; }
      100% { transform: translate(-50%, -50%) scale(2.5); opacity: 0; }
    }
  `;
  document.head.appendChild(style);

  const cursor = document.createElement("div");
  cursor.id = "__pw-cursor";
  cursor.setAttribute("data-pw-overlay", "");
  document.body.appendChild(cursor);

  window.__PW_CURSOR_VIS__ = true;
}

// --- Move cursor + interpolate trail ---
// pos: [x, y]
function __pwCursorMove(pos) {
  const el = document.getElementById("__pw-cursor");
  if (!el || !document.body) return;

  if (!window.__pw_trails) window.__pw_trails = [];

  const hasPrev = el.style.left !== "" && el.style.top !== "";
  const prevX = parseFloat(el.style.left) || 0;
  const prevY = parseFloat(el.style.top) || 0;
  const newX = pos[0],
    newY = pos[1];
  const dx = newX - prevX,
    dy = newY - prevY;
  const dist = Math.sqrt(dx * dx + dy * dy);

  // Place a dot every ~8px along the path (skip first move from origin)
  const steps = hasPrev ? Math.max(1, Math.floor(dist / 8)) : 0;
  for (let i = 0; i < steps; i++) {
    const t = steps === 1 ? 0 : i / steps;
    const x = prevX + dx * t;
    const y = prevY + dy * t;

    if (window.__pw_trails.length >= 120) {
      const old = window.__pw_trails.shift();
      if (old && old.parentNode) old.remove();
    }
    const dot = document.createElement("div");
    dot.setAttribute("data-pw-overlay", "");
    Object.assign(dot.style, {
      position: "fixed",
      left: x + "px",
      top: y + "px",
      width: "6px",
      height: "6px",
      borderRadius: "50%",
      background: "rgba(255, 68, 68, 0.45)",
      pointerEvents: "none",
      zIndex: "2147483646",
      transform: "translate(-50%, -50%)",
      animation: "__pw-trail-fade 0.8s ease-out forwards",
    });
    document.body.appendChild(dot);
    window.__pw_trails.push(dot);
    dot.addEventListener("animationend", () => {
      dot.remove();
      const idx = window.__pw_trails.indexOf(dot);
      if (idx !== -1) window.__pw_trails.splice(idx, 1);
    });
  }

  el.style.left = newX + "px";
  el.style.top = newY + "px";
}

// --- Click ring animation ---
// pos: [x, y]
function __pwCursorClickRing(pos) {
  if (!document.body) return;
  const ring = document.createElement("div");
  ring.setAttribute("data-pw-overlay", "");
  Object.assign(ring.style, {
    position: "fixed",
    left: pos[0] + "px",
    top: pos[1] + "px",
    width: "40px",
    height: "40px",
    borderRadius: "50%",
    border: "3px solid rgba(255, 68, 68, 0.8)",
    pointerEvents: "none",
    zIndex: "2147483647",
    transform: "translate(-50%, -50%) scale(0.5)",
    animation: "__pw-click-ring 0.4s ease-out forwards",
  });
  document.body.appendChild(ring);
  ring.addEventListener("animationend", () => ring.remove());
  setTimeout(() => {
    if (ring.parentNode) ring.remove();
  }, 600);
}

// --- Hide/show all overlay elements (for screenshots) ---
function __pwCursorHide() {
  document.querySelectorAll("[data-pw-overlay]").forEach((el) => {
    el.style.display = "none";
  });
}

function __pwCursorShow() {
  document.querySelectorAll("[data-pw-overlay]").forEach((el) => {
    el.style.display = "";
  });
}
