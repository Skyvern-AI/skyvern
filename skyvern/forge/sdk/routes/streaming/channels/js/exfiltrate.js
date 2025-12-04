(function () {
  console.log("[SYS] exfiltration: evaluated");
  if (!window.__skyvern_exfiltration_initialized) {
    console.log("[SYS] exfiltration: initializing");
    window.__skyvern_exfiltration_initialized = true;

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
            },
            inputValue: ["input", "focus", "blur"].includes(eventType)
              ? e.target?.value
              : undefined,
            mousePosition: {
              xa: e.clientX,
              ya: e.clientY,
              xp: e.clientX / window.innerWidth,
              yp: e.clientY / window.innerHeight,
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

          console.log("[EXFIL]", JSON.stringify(eventData));
        },
        true,
      );
    });
  }
})();
