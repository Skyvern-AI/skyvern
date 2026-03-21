() => {
  const fields = [];
  const seen = new Set();

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
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref) return ref.textContent.trim();
    }
    if (el.placeholder) return el.placeholder;
    return null;
  }

  function buildSelector(el, label) {
    const tag = el.tagName.toLowerCase();
    const elType = (el.getAttribute("type") || "").toLowerCase();
    const vis = elType === "file" ? "" : ":visible";
    if (el.name) return tag + '[name="' + el.name + '"]' + vis;
    if (el.id) return "#" + el.id + vis;
    if (label && label.length < 80) {
      const escapedLabel = label.replace(/"/g, '\\"').replace(/'/g, "\\'");
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
    if (el.id) return "#" + el.id;
    const tag = el.tagName.toLowerCase();
    const name = el.name;
    const value = el.value;
    if (name && value)
      return tag + '[name="' + name + '"][value="' + value + '"]';
    if (name) return tag + '[name="' + name + '"]';
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
    if (type !== "file" && !isVisible(el)) continue;

    if (type === "checkbox" || type === "radio") {
      if (el.name) {
        if (!checkRadioGroups[el.name]) {
          checkRadioGroups[el.name] = { type: type, elements: [] };
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
              type +
              "_" +
              Math.random().toString(36).slice(2, 8);
          }
          const gk = container._groupKey;
          if (!checkRadioGroups[gk]) {
            checkRadioGroups[gk] = { type: type, elements: [] };
          }
          if (!checkRadioGroups[gk].elements.includes(el)) {
            checkRadioGroups[gk].elements.push(el);
          }
        } else {
          const label = getLabel(el);
          const selector = buildSelector(el, label);
          if (selector) {
            fields.push({
              label: label || null,
              selector: selector,
              tag: "input",
              type: type,
              name: null,
              required: el.required || false,
              placeholder: null,
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
      Math.random().toString();
    if (seen.has(uid)) continue;
    seen.add(uid);

    const label = getLabel(el);
    const selector = buildSelector(el, label);
    if (!selector) continue;

    fields.push({
      label: label || null,
      selector: selector,
      tag: el.tagName.toLowerCase(),
      type:
        type ||
        (el.tagName.toLowerCase() === "select"
          ? "select"
          : el.tagName.toLowerCase() === "textarea"
            ? "textarea"
            : "text"),
      name: el.name || null,
      required: el.required || false,
      placeholder: el.placeholder || null,
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
    if (!firstSelector) continue;

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
  return fields;
};
