(function () {
  console.log("[SYS] exfiltration: evaluated");
  if (!window.__skyvern_exfiltration_initialized) {
    console.log("[SYS] exfiltration: initializing");
    window.__skyvern_exfiltration_initialized = true;

    // Event types whose targets feed the recording state machines; only these
    // pay for durable-locator computation (querySelectorAll uniqueness probes).
    const LOCATOR_EVENT_TYPES = new Set([
      "click",
      "dblclick",
      "contextmenu",
      "focus",
      "blur",
      "input",
      "change",
      "keydown",
      "mouseenter",
    ]);

    const cssEscape =
      window.CSS && window.CSS.escape
        ? window.CSS.escape.bind(window.CSS)
        : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);

    // The capture listener has no surrounding try/catch and this script is
    // injected for every recording, so locator computation must degrade to
    // null instead of breaking event capture.
    const tryCapture = (fn) => {
      try {
        return fn();
      } catch (err) {
        return null;
      }
    };

    const isUniqueSelector = (selector) => {
      try {
        return document.querySelectorAll(selector).length === 1;
      } catch (err) {
        return false;
      }
    };

    // Framework-generated ids (numeric suffixes, uuid-ish, React ":r1:") churn
    // across sessions and must not become replay selectors.
    const looksGeneratedId = (id) =>
      /^\d|[_-]\d{3,}|^[0-9a-fA-F-]{16,}$|^:|:$/.test(id);

    const TEST_ATTRIBUTES = [
      "data-testid",
      "data-test-id",
      "data-test",
      "data-qa",
      "data-cy",
    ];

    const getDurableSelector = (element) => {
      if (!element || !element.tagName || !element.getAttribute) {
        return null;
      }

      const id = element.id;
      if (id && !looksGeneratedId(id)) {
        const selector = `#${cssEscape(id)}`;
        if (isUniqueSelector(selector)) return selector;
      }

      for (const attribute of TEST_ATTRIBUTES) {
        const value = element.getAttribute(attribute);
        if (value) {
          const selector = `[${attribute}="${cssEscape(value)}"]`;
          if (isUniqueSelector(selector)) return selector;
        }
      }

      const tag = element.tagName.toLowerCase();
      if (["input", "select", "textarea", "button"].includes(tag)) {
        const name = element.getAttribute("name");
        if (name) {
          const selector = `${tag}[name="${cssEscape(name)}"]`;
          if (isUniqueSelector(selector)) return selector;
        }
      }

      // Structural fallback: tag + :nth-of-type at each level, anchored at the
      // nearest stable-id ancestor (or body). Unique by construction; brittle
      // under reordering, so it is last.
      const segments = [];
      let current = element;
      let anchor = "";
      let depth = 0;
      while (current && current.tagName && depth < 8) {
        const currentTag = current.tagName.toLowerCase();
        if (currentTag === "body" || currentTag === "html") break;
        const currentId = current.id;
        if (
          depth > 0 &&
          currentId &&
          !looksGeneratedId(currentId) &&
          isUniqueSelector(`#${cssEscape(currentId)}`)
        ) {
          anchor = `#${cssEscape(currentId)}`;
          break;
        }
        let nth = 1;
        let sibling = current.previousElementSibling;
        while (sibling) {
          if (sibling.tagName === current.tagName) nth += 1;
          sibling = sibling.previousElementSibling;
        }
        segments.unshift(`${currentTag}:nth-of-type(${nth})`);
        current = current.parentElement;
        depth += 1;
      }
      // Empty only when the target is body/html itself, which is never a
      // useful replay target.
      if (!segments.length) return null;
      const selector = `${anchor ? `${anchor} > ` : ""}${segments.join(" > ")}`;
      return isUniqueSelector(selector) ? selector : null;
    };

    const IMPLICIT_INPUT_ROLES = {
      button: "button",
      submit: "button",
      reset: "button",
      image: "button",
      checkbox: "checkbox",
      radio: "radio",
      range: "slider",
      number: "spinbutton",
      search: "searchbox",
    };

    const getAriaRole = (element) => {
      if (!element || !element.tagName || !element.getAttribute) {
        return null;
      }

      const explicit = element.getAttribute("role");
      if (explicit) return explicit.trim().split(/\s+/)[0];

      const tag = element.tagName.toLowerCase();
      switch (tag) {
        case "a":
          return element.hasAttribute("href") ? "link" : null;
        case "button":
        case "summary":
          return "button";
        case "select":
          return element.multiple || element.size > 1 ? "listbox" : "combobox";
        case "textarea":
          return "textbox";
        case "option":
          return "option";
        case "img":
          return "img";
        case "input": {
          const type = (element.getAttribute("type") || "text").toLowerCase();
          if (type === "hidden") return null;
          return IMPLICIT_INPUT_ROLES[type] || "textbox";
        }
        default:
          return null;
      }
    };

    [
      "click",
      "mousedown",
      "mouseup",
      "mouseenter",
      "mouseleave",
      "keydown",
      "keyup",
      "keypress",
      "focus",
      "blur",
      "input",
      "change",
      "scroll",
      "contextmenu",
      "dblclick",
    ].forEach((eventType) => {
      document.addEventListener(
        eventType,
        (e) => {
          // find associated labels
          const getAssociatedLabels = (element) => {
            const labels = [];

            // label with 'for' attribute matching element's id
            if (element.id) {
              const labelsByFor = document.querySelectorAll(
                `label[for="${element.id}"]`,
              );

              labelsByFor.forEach((label) => {
                if (label.textContent) labels.push(label.textContent.trim());
              });
            }

            // label wrapping the element
            let parent = element.parentElement;

            while (parent) {
              if (parent.tagName === "LABEL") {
                if (parent.textContent) labels.push(parent.textContent.trim());
                break;
              }
              parent = parent.parentElement;
            }

            return labels.length > 0 ? labels : null;
          };

          // get any kind of text content
          const getElementText = (element) => {
            const textSources = [];

            if (!element.getAttribute) {
              return textSources;
            }

            if (element.getAttribute("aria-label")) {
              textSources.push(element.getAttribute("aria-label"));
            }

            if (element.getAttribute("aria-labelledby")) {
              const labelIds = element
                .getAttribute("aria-labelledby")
                .split(" ");

              labelIds.forEach((id) => {
                const labelElement = document.getElementById(id);

                if (labelElement?.textContent) {
                  textSources.push(labelElement.textContent.trim());
                }
              });
            }

            if (element.getAttribute("placeholder")) {
              textSources.push(element.getAttribute("placeholder"));
            }

            if (element.getAttribute("title")) {
              textSources.push(element.getAttribute("title"));
            }

            if (element.getAttribute("alt")) {
              textSources.push(element.getAttribute("alt"));
            }

            if (element.innerText) {
              textSources.push(element.innerText.substring(0, 100));
            }

            if (!element.innerText && element.textContent) {
              textSources.push(element.textContent.trim().substring(0, 100));
            }

            return textSources.length > 0 ? textSources : [];
          };

          // Approximate accname computation, in spec priority order; Playwright's
          // get_by_role(name=...) resolves the same sources for common controls.
          const getAccessibleName = (element) => {
            if (!element || !element.getAttribute) {
              return null;
            }

            const candidates = [];

            const ariaLabel = element.getAttribute("aria-label");
            if (ariaLabel) candidates.push(ariaLabel);

            const labelledBy = element.getAttribute("aria-labelledby");
            if (labelledBy) {
              const labelText = labelledBy
                .split(/\s+/)
                .map((id) => document.getElementById(id)?.textContent || "")
                .join(" ");
              candidates.push(labelText);
            }

            const labels = getAssociatedLabels(element);
            if (labels) candidates.push(labels[0]);

            const tag = element.tagName?.toLowerCase();
            if (["button", "a", "summary", "option"].includes(tag)) {
              candidates.push(element.innerText || element.textContent);
            }
            if (tag === "input") {
              const type = (element.getAttribute("type") || "").toLowerCase();
              if (["submit", "button", "reset"].includes(type)) {
                candidates.push(element.value);
              }
            }

            candidates.push(element.getAttribute("title"));
            candidates.push(element.getAttribute("alt"));
            candidates.push(element.getAttribute("placeholder"));

            for (const candidate of candidates) {
              const name = String(candidate || "")
                .replace(/\s+/g, " ")
                .trim();
              if (name) return name.substring(0, 100);
            }

            return null;
          };

          const wantsLocator = LOCATOR_EVENT_TYPES.has(eventType);

          const skyId = e.target?.dataset?.skyId || null;

          if (!skyId && e.target?.tagName !== "HTML") {
            console.log("[SYS] exfiltration: target element has no skyId.");

            if (window.__skyvern_generateUniqueId && e.target?.dataset) {
              const newSkyId = window.__skyvern_generateUniqueId();
              e.target.dataset.skyId = newSkyId;
              console.log(
                `[SYS] exfiltration: assigned new skyId to target element: ${newSkyId}`,
              );
            } else {
              console.log(
                "[SYS] exfiltration: cannot assign skyId, generator not found.",
              );

              const info = {
                tagName: e.target?.tagName,
                target: e.target,
                targetType: typeof e.target,
                eventType,
                id: e.target?.id,
                className: e.target?.className,
                value: e.target?.value,
                text: getElementText(e.target),
                labels: getAssociatedLabels(e.target),
                skyId: e.target?.dataset?.skyId,
              };

              try {
                const infoS = JSON.stringify(info, null, 2);
                console.log(
                  `[SYS] exfiltration: target element info: ${infoS}`,
                );
              } catch (err) {
                console.log(
                  "[SYS] exfiltration: target element info: [unserializable]",
                );
              }
            }
          }

          const classText = String(
            e.target.classList?.value ?? e.target.getAttribute("class") ?? "",
          );

          const eventData = {
            url: window.location.href,
            type: eventType,
            timestamp: Date.now(),
            target: {
              tagName: e.target?.tagName,
              id: e.target?.id,
              isHtml: e.target instanceof HTMLElement,
              isSvg: e.target instanceof SVGElement,
              className: classText,
              value: e.target?.value,
              text: getElementText(e.target),
              labels: getAssociatedLabels(e.target),
              skyId: e.target?.dataset?.skyId,
              selector: wantsLocator
                ? tryCapture(() => getDurableSelector(e.target))
                : null,
              role: wantsLocator
                ? tryCapture(() => getAriaRole(e.target))
                : null,
              accessibleName: wantsLocator
                ? tryCapture(() => getAccessibleName(e.target))
                : null,
              inputType:
                e.target?.tagName === "INPUT" ||
                e.target?.tagName === "SELECT" ||
                e.target?.tagName === "TEXTAREA" ||
                e.target?.tagName === "BUTTON"
                  ? e.target?.type || null
                  : null,
            },
            inputValue: ["input", "focus", "blur"].includes(eventType)
              ? e.target?.value
              : undefined,
            mousePosition: {
              xa: Number.isFinite(e.clientX) ? e.clientX : null,
              ya: Number.isFinite(e.clientY) ? e.clientY : null,
              xp:
                Number.isFinite(e.clientX) && window.innerWidth
                  ? e.clientX / window.innerWidth
                  : null,
              yp:
                Number.isFinite(e.clientY) && window.innerHeight
                  ? e.clientY / window.innerHeight
                  : null,
            },
            key: e.key,
            code: e.code,
            activeElement: {
              tagName: document.activeElement?.tagName,
              id: document.activeElement?.id,
              className: document.activeElement?.className,
              boundingRect: document.activeElement?.getBoundingClientRect
                ? {
                    x: document.activeElement.getBoundingClientRect().x,
                    y: document.activeElement.getBoundingClientRect().y,
                    width: document.activeElement.getBoundingClientRect().width,
                    height:
                      document.activeElement.getBoundingClientRect().height,
                    top: document.activeElement.getBoundingClientRect().top,
                    right: document.activeElement.getBoundingClientRect().right,
                    bottom:
                      document.activeElement.getBoundingClientRect().bottom,
                    left: document.activeElement.getBoundingClientRect().left,
                  }
                : null,
              scroll: document.activeElement
                ? {
                    scrollTop: document.activeElement.scrollTop,
                    scrollLeft: document.activeElement.scrollLeft,
                    scrollHeight: document.activeElement.scrollHeight,
                    scrollWidth: document.activeElement.scrollWidth,
                    clientHeight: document.activeElement.clientHeight,
                    clientWidth: document.activeElement.clientWidth,
                  }
                : null,
            },
            window: {
              width: window.innerWidth,
              height: window.innerHeight,
              scrollX: window.scrollX,
              scrollY: window.scrollY,
            },
          };

          const bindingName = window.__skyvern_exfiltration_binding_name;
          const binding =
            typeof bindingName === "string" ? window[bindingName] : null;

          // Single transport: prefer the CDP binding; console.log is only the
          // fallback when the binding is absent. Sending through both delivered
          // every event 2-3x (the console paths also cost json_value CDP
          // round-trips per event, queueing seconds of latency under load).
          if (typeof binding === "function") {
            Promise.resolve(binding(eventData)).catch((err) => {
              console.log("[SYS] exfiltration: binding transport failed.", err);
              console.log("[EXFIL]", JSON.stringify(eventData));
            });
          } else {
            console.log("[EXFIL]", JSON.stringify(eventData));
          }
        },
        true,
      );
    });
  }
})();
