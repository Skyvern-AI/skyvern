() => {
  const fields = [];
  const seen = new Set();
  let _counter = 0;

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.opacity !== "0" &&
      el.offsetWidth > 0 &&
      el.offsetHeight > 0
    );
  }

  function getLabel(el) {
    if (el.id) {
      const lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    const parentLabel = el.closest("label");
    if (parentLabel) return parentLabel.textContent.trim();
    const ariaLabel = el.getAttribute("aria-label");
    // fieldset/legend — prefer over generic aria-labels like "Month", "Day", "Year"
    // which don't tell the LLM WHICH date is being asked
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) {
        const legendText = legend.textContent.trim();
        if (legendText && legendText.length > 3 && legendText.length < 300) {
          // If aria-label exists but is short/generic, prepend it to the legend
          // so the LLM knows both the question and the field role
          if (ariaLabel && ariaLabel.length < 15) {
            return ariaLabel + " — " + legendText;
          }
          return legendText;
        }
      }
    }
    if (ariaLabel) return ariaLabel;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref) return ref.textContent.trim();
    }
    if (el.placeholder) return el.placeholder;
    // Custom aria-checked inputs have sibling labels, not parent labels
    if (el.hasAttribute && el.hasAttribute("aria-checked")) {
      for (const sib of [el.previousElementSibling, el.nextElementSibling]) {
        if (sib && sib.tagName === "LABEL") return sib.textContent.trim();
      }
      const parent = el.parentElement;
      if (parent) {
        const lbl = parent.querySelector("label");
        if (lbl) return lbl.textContent.trim();
      }
    }
    // Walk up to 4 ancestors checking for a preceding sibling with
    // question/label text (common in multi-part form layouts)
    let prevSib = el.parentElement;
    for (let walk = 0; walk < 4 && prevSib; walk++) {
      const prev = prevSib.previousElementSibling;
      if (prev) {
        const t = prev.textContent.trim();
        if (t && t.length > 3 && t.length < 300) return t;
      }
      prevSib = prevSib.parentElement;
    }
    // Last resort: look at the element's title attribute
    if (el.title) return el.title;
    return null;
  }

  function buildSelector(el, label) {
    const tag = el.tagName.toLowerCase();
    const elType = (el.getAttribute("type") || "").toLowerCase();
    const vis = elType === "file" ? "" : ":visible";
    if (el.name) return tag + '[name="' + el.name + '"]' + vis;
    if (el.id) return "#" + CSS.escape(el.id) + vis;
    // File inputs with no name/id: no name/id, use data-automation-id or type selector
    if (elType === "file") {
      const autoId = el.getAttribute("data-automation-id");
      if (autoId) return tag + '[data-automation-id="' + autoId + '"]';
      return 'input[type="file"]';
    }
    if (label && label.length < 80) {
      const escapedLabel = label
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"')
        .replace(/'/g, "\\'");
      const parentLabel = el.closest("label");
      if (
        parentLabel ||
        (el.id && document.querySelector('label[for="' + el.id + '"]'))
      ) {
        return "label:has-text('" + escapedLabel + "') " + tag + ":visible";
      }
      if (el.getAttribute("aria-label")) {
        return tag + '[aria-label="' + escapedLabel + '"]:visible';
      }
    }
    return null;
  }

  function buildOptionSelector(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    const name = el.name;
    const value = el.value;
    if (name && value)
      return tag + '[name="' + name + '"][value="' + value + '"]';
    if (name) return tag + '[name="' + name + '"]';
    // Anonymous <input aria-checked> — find the associated label and
    // build a selector. Structure varies: label may wrap input, be a sibling, or
    // both may be inside a <div role="group">.
    if (el.hasAttribute("aria-checked")) {
      // 1. Label wraps input: <label>Yes<input></label>
      const parentLabel = el.closest("label");
      if (parentLabel) {
        const labelText = parentLabel.textContent.trim();
        if (labelText && labelText.length < 50) {
          return (
            "label:has-text('" +
            labelText.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
            "') input"
          );
        }
      }
      // 2. Label is a sibling (before or after): <label>Yes</label><input>
      // Click the LABEL (visible) instead of the hidden input — the UI
      // responds to label clicks, not hidden input clicks
      for (const sibling of [
        el.previousElementSibling,
        el.nextElementSibling,
      ]) {
        if (sibling && sibling.tagName === "LABEL") {
          const labelText = sibling.textContent.trim();
          if (labelText && labelText.length < 50) {
            return (
              "label:has-text('" +
              labelText.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
              "')"
            );
          }
        }
      }
      // 3. Label is inside the same container: <div role="group"><label>Yes</label>...<input></div>
      const container = el.parentElement;
      if (container) {
        const labels = container.querySelectorAll("label");
        const inputIndex = Array.from(container.children).indexOf(el);
        let bestLabel = null;
        let bestDist = Infinity;
        for (const lbl of labels) {
          const lblIndex = Array.from(container.children).indexOf(lbl);
          const dist = Math.abs(lblIndex - inputIndex);
          if (dist < bestDist) {
            bestDist = dist;
            bestLabel = lbl;
          }
        }
        if (bestLabel) {
          const labelText = bestLabel.textContent.trim();
          if (labelText && labelText.length < 50) {
            return (
              "label:has-text('" +
              labelText.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
              "')"
            );
          }
        }
      }
    }
    return null;
  }

  function getGroupLabel(elements) {
    if (!elements.length) return null;
    const first = elements[0];

    const fieldset = first.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) return legend.textContent.trim();
    }

    let questionContainer = first.parentElement;
    for (let i = 0; i < 6 && questionContainer; i++) {
      const labelEl = questionContainer.querySelector(
        ".application-label, .question-label, .field-label, " +
          '[class*="label"]:not(label), legend, ' +
          '[data-qa="question-text"], [class*="question-text"]',
      );
      if (labelEl) {
        const text = labelEl.textContent.trim();
        if (
          text &&
          text.length < 300 &&
          elements.some((el) => questionContainer.contains(el))
        ) {
          return text;
        }
      }
      questionContainer = questionContainer.parentElement;
    }

    let ancestor = first.parentElement;
    const allInAncestor = () =>
      elements.every((el) => ancestor && ancestor.contains(el));
    while (ancestor && !allInAncestor()) {
      ancestor = ancestor.parentElement;
    }
    if (ancestor) {
      if (ancestor.getAttribute("aria-label"))
        return ancestor.getAttribute("aria-label");
      const lblBy = ancestor.getAttribute("aria-labelledby");
      if (lblBy) {
        const ref = document.getElementById(lblBy);
        if (ref) return ref.textContent.trim();
      }
      let prev = ancestor.previousElementSibling;
      while (prev) {
        const tagName = prev.tagName.toLowerCase();
        if (
          [
            "label",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "p",
            "span",
            "div",
          ].includes(tagName)
        ) {
          const text = prev.textContent.trim();
          if (text && text.length > 3 && text.length < 300) return text;
        }
        prev = prev.previousElementSibling;
      }
    }

    return getLabel(first);
  }

  const elements = document.querySelectorAll("input, select, textarea");
  const checkRadioGroups = {};

  for (const el of elements) {
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (["hidden", "submit", "button", "image", "reset"].includes(type))
      continue;
    // Skip inputs inside multiselect containers — platform extension handles those
    if (el.closest('[data-uxi-widget-type="multiselect"]')) continue;
    // Allow aria-checked inputs through even if visually hidden (hides
    // real radio/checkbox inputs behind styled overlays at opacity:0)
    const hasAriaChecked = el.hasAttribute("aria-checked");
    if (type !== "file" && !hasAriaChecked && !isVisible(el)) continue;

    // Detect special input types by ARIA attributes:
    // - aria-checked → custom radio
    // - role="combobox" → search dropdown (type to filter, click to select)
    const effectiveType =
      type === "checkbox" || type === "radio"
        ? type
        : el.hasAttribute("aria-checked")
          ? "radio"
          : el.getAttribute("role") === "combobox"
            ? "search-dropdown"
            : type;

    if (effectiveType === "checkbox" || effectiveType === "radio") {
      if (el.name) {
        if (!checkRadioGroups[el.name]) {
          checkRadioGroups[el.name] = { type: effectiveType, elements: [] };
        }
        checkRadioGroups[el.name].elements.push(el);
      } else {
        let container = el.parentElement;
        while (container && container !== document.body) {
          const siblings = container.querySelectorAll(
            'input[type="' + type + '"]',
          );
          if (siblings.length >= 2) break;
          container = container.parentElement;
        }
        if (container && container !== document.body) {
          if (!container._groupKey) {
            container._groupKey =
              "__nameless_" +
              effectiveType +
              "_" +
              Math.random().toString(36).slice(2, 8);
          }
          const gk = container._groupKey;
          if (!checkRadioGroups[gk]) {
            checkRadioGroups[gk] = { type: effectiveType, elements: [] };
          }
          if (!checkRadioGroups[gk].elements.includes(el)) {
            checkRadioGroups[gk].elements.push(el);
          }
        } else {
          // Standalone checkbox/radio with no siblings — this is a toggle
          // (e.g., "I have a preferred name", "SMS opt-in") that reveals or
          // enables a section.  Present as "toggle" so the LLM knows to leave
          // it unchecked unless data explicitly requires it.
          const label = getLabel(el);
          const selector = buildSelector(el, label);
          if (selector) {
            const isChecked =
              el.getAttribute("aria-checked") === "true" || el.checked;
            fields.push({
              label: label || null,
              selector: selector,
              tag: "input",
              type: "toggle",
              name: null,
              required: el.required || false,
              placeholder: null,
              currentValue: isChecked ? "checked" : "unchecked",
            });
          }
        }
      }
      continue;
    }

    const uid =
      el.name ||
      el.id ||
      el.getAttribute("aria-label") ||
      "__anon_" + _counter++;
    if (seen.has(uid)) continue;
    seen.add(uid);

    const label = getLabel(el);
    const selector = buildSelector(el, label);
    if (!selector) continue;

    // Collect format hints for phone/date fields — validation messages,
    // pattern attributes, and aria-describedby text that tell the LLM
    // what format the field expects (e.g., "(xxx) xxx-xxxx").
    let formatHint = null;
    const autoId = el.getAttribute("data-automation-id") || "";
    const elName = (el.name || "").toLowerCase();
    const elLabel = (label || "").toLowerCase();
    const isPhoneOrDate =
      elName.includes("phone") ||
      elLabel.includes("phone") ||
      autoId.includes("phone") ||
      elName.includes("date") ||
      elLabel.includes("date") ||
      autoId.includes("date");
    if (isPhoneOrDate || el.pattern) {
      const hints = [];
      if (el.pattern) hints.push("pattern: " + el.pattern);
      if (el.title) hints.push(el.title);
      // aria-describedby often links to a format description element
      const describedBy = el.getAttribute("aria-describedby");
      if (describedBy) {
        for (const refId of describedBy.split(/\s+/)) {
          const ref = document.getElementById(refId);
          if (ref) {
            const t = ref.textContent.trim();
            if (t && t.length < 200) hints.push(t);
          }
        }
      }
      // Check closest form-group wrapper for format/help text
      const wrapper =
        el.closest(".form-group") ||
        el.closest('[role="group"]') ||
        el.closest("fieldset");
      if (wrapper) {
        // Look for elements that contain format hints (e.g., "(xxx) xxx-xxxx")
        const hintEl = wrapper.querySelector(
          '.help-text, .hint, [class*="hint"], [class*="help"]',
        );
        if (hintEl) {
          const t = hintEl.textContent.trim();
          if (t && t.length < 200) hints.push(t);
        }
      }
      if (hints.length > 0) formatHint = hints.join(" | ");
    }

    fields.push({
      label: label || null,
      selector: selector,
      tag: el.tagName.toLowerCase(),
      type:
        effectiveType ||
        (el.tagName.toLowerCase() === "select"
          ? "select"
          : el.tagName.toLowerCase() === "textarea"
            ? "textarea"
            : "text"),
      name: el.name || null,
      required: el.required || false,
      placeholder: el.placeholder || null,
      formatHint: formatHint,
    });

    if (el.tagName.toLowerCase() === "select") {
      const selectOptions = [];
      for (const opt of el.options) {
        const optText = opt.textContent.trim();
        if (
          !opt.value ||
          opt.value === "" ||
          optText === "" ||
          optText === "--"
        )
          continue;
        selectOptions.push({
          label: optText,
          value: opt.value,
        });
      }
      if (selectOptions.length > 0) {
        fields[fields.length - 1].options = selectOptions;
      }
    }
  }

  for (const [groupKey, group] of Object.entries(checkRadioGroups)) {
    const els = group.elements;
    if (seen.has(groupKey)) continue;
    seen.add(groupKey);

    const groupLabel = getGroupLabel(els);
    const firstSelector =
      buildOptionSelector(els[0]) || buildSelector(els[0], getLabel(els[0]));
    if (!firstSelector) {
      // Fallback for aria-checked groups: use nth-of-type selectors
      if (
        els[0].hasAttribute &&
        els[0].hasAttribute("aria-checked") &&
        els.length >= 2
      ) {
        const fallbackOptions = [];
        for (let oi = 0; oi < els.length; oi++) {
          const optEl = els[oi];
          const optLabel =
            getLabel(optEl) || optEl.value || "Option " + (oi + 1);
          // Build selector by walking up to the label
          const parentLabel = optEl.closest("label");
          let optSelector = null;
          if (parentLabel) {
            const lt = parentLabel.textContent.trim();
            if (lt && lt.length < 50) {
              optSelector =
                "label:has-text('" +
                lt.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
                "')";
            }
          }
          if (!optSelector && optEl.id)
            optSelector = "#" + CSS.escape(optEl.id);
          if (optSelector) {
            fallbackOptions.push({
              label: optLabel,
              value: optLabel,
              selector: optSelector,
            });
          }
        }
        if (fallbackOptions.length >= 2) {
          fields.push({
            label: groupLabel || null,
            selector: fallbackOptions[0].selector,
            tag: "input",
            type: "radio_group",
            name: groupKey,
            required: false,
            placeholder: null,
            options: fallbackOptions,
          });
        }
      }
      continue;
    }

    const options = [];
    for (const el of els) {
      const optLabel = getLabel(el) || el.value || null;
      const optSelector = buildOptionSelector(el);
      if (!optSelector) continue;
      options.push({
        label: optLabel,
        value: el.value || null,
        selector: optSelector,
      });
    }

    const groupType = group.type === "radio" ? "radio_group" : "checkbox_group";
    fields.push({
      label: groupLabel || null,
      selector: firstSelector,
      tag: "input",
      type: groupType,
      name: els[0].name || null,
      required: els[0].required || false,
      placeholder: null,
      options: options,
    });
  }
  // Pass 3: Detect custom ARIA role-based components
  // These are <div role="radio">, <div role="checkbox">, <div role="listbox"> etc.
  // that standard input scanning misses.

  // 3a: Custom radio groups via [role="radiogroup"] or grouped [role="radio"]
  const ariaRadioGroups = document.querySelectorAll('[role="radiogroup"]');
  for (const group of ariaRadioGroups) {
    const radios = group.querySelectorAll('[role="radio"]');
    if (radios.length === 0) continue;
    const groupId =
      group.getAttribute("data-automation-id") ||
      group.id ||
      Math.random().toString(36).slice(2, 8);
    if (seen.has("aria_rg_" + groupId)) continue;
    seen.add("aria_rg_" + groupId);

    const groupLabel = getGroupLabel(Array.from(radios));
    const options = [];
    for (const r of radios) {
      const optLabel =
        r.getAttribute("aria-label") || r.textContent.trim() || null;
      const optSelector = r.getAttribute("data-automation-id")
        ? '[data-automation-id="' + r.getAttribute("data-automation-id") + '"]'
        : r.id
          ? "#" + CSS.escape(r.id)
          : null;
      if (optSelector) {
        options.push({
          label: optLabel,
          value: optLabel,
          selector: optSelector,
        });
      }
    }
    if (options.length > 0) {
      fields.push({
        label: groupLabel || null,
        selector: options[0].selector,
        tag: "div",
        type: "radio_group",
        name: groupId,
        required: group.getAttribute("aria-required") === "true",
        placeholder: null,
        options: options,
      });
    }
  }

  // 3b: Ungrouped [role="radio"] elements (not inside a radiogroup)
  const orphanRadios = document.querySelectorAll(
    '[role="radio"]:not([role="radiogroup"] [role="radio"])',
  );
  if (orphanRadios.length > 0) {
    // Group orphan radios by their nearest common ancestor
    const orphanGroups = {};
    for (const r of orphanRadios) {
      if (!isVisible(r)) continue;
      // Walk up to find a grouping container
      let container = r.parentElement;
      for (let i = 0; i < 5 && container; i++) {
        const siblings = container.querySelectorAll('[role="radio"]');
        if (siblings.length >= 2) break;
        container = container.parentElement;
      }
      const key = container
        ? container.getAttribute("data-automation-id") ||
          container.id ||
          "orphan_" + Math.random().toString(36).slice(2, 6)
        : "orphan_default";
      if (!orphanGroups[key])
        orphanGroups[key] = { container: container, elements: [] };
      if (!orphanGroups[key].elements.includes(r))
        orphanGroups[key].elements.push(r);
    }
    for (const [key, group] of Object.entries(orphanGroups)) {
      if (seen.has("aria_or_" + key)) continue;
      seen.add("aria_or_" + key);
      const groupLabel = getGroupLabel(group.elements);
      const options = [];
      for (const r of group.elements) {
        const optLabel =
          r.getAttribute("aria-label") || r.textContent.trim() || null;
        const optSelector = r.getAttribute("data-automation-id")
          ? '[data-automation-id="' +
            r.getAttribute("data-automation-id") +
            '"]'
          : r.id
            ? "#" + CSS.escape(r.id)
            : null;
        if (optSelector) {
          options.push({
            label: optLabel,
            value: optLabel,
            selector: optSelector,
          });
        }
      }
      if (options.length > 0) {
        fields.push({
          label: groupLabel || null,
          selector: options[0].selector,
          tag: "div",
          type: "radio_group",
          name: key,
          required: false,
          placeholder: null,
          options: options,
        });
      }
    }
  }

  // 3c: Custom [role="checkbox"] elements (not <input type="checkbox">)
  const ariaCheckboxes = document.querySelectorAll(
    '[role="checkbox"]:not(input)',
  );
  for (const cb of ariaCheckboxes) {
    if (!isVisible(cb)) continue;
    const cbId = cb.getAttribute("data-automation-id") || cb.id || null;
    if (!cbId || seen.has("aria_cb_" + cbId)) continue;
    seen.add("aria_cb_" + cbId);
    const label =
      cb.getAttribute("aria-label") ||
      getLabel(cb) ||
      cb.textContent.trim() ||
      null;
    const selector = cbId
      ? '[data-automation-id="' + cbId + '"]'
      : cb.id
        ? "#" + CSS.escape(cb.id)
        : null;
    if (selector) {
      fields.push({
        label: label,
        selector: selector,
        tag: "div",
        type: "checkbox",
        name: cbId,
        required: cb.getAttribute("aria-required") === "true",
        placeholder: null,
      });
    }
  }

  // PLATFORM_EXTENSION_POINT

  return fields;
};
