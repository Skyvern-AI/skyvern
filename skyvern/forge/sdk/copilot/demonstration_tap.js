(() => {
  if (
    globalThis.__skyvernDemoTap?.document === document &&
    !globalThis.__skyvernDemoTap.stopped
  )
    return;

  // Inputs that were type=password when the tap saw them; a show-password toggle keeps them secret.
  const knownPasswords = new WeakSet();
  const cssEscape =
    globalThis.CSS && globalThis.CSS.escape
      ? globalThis.CSS.escape.bind(globalThis.CSS)
      : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
  const isUniqueSelector = (selector, root = document) => {
    try {
      return root.querySelectorAll(selector).length === 1;
    } catch (err) {
      return false;
    }
  };
  const looksGeneratedId = (id) =>
    /^\d|[_-]\d{3,}|^[0-9a-fA-F-]{16,}$|^:|:$/.test(id);
  const TEST_ATTRIBUTES = [
    "data-testid",
    "data-test-id",
    "data-test",
    "data-qa",
    "data-cy",
  ];
  const selectorWithinRoot = (element, root) => {
    if (!element || !element.tagName || !element.getAttribute) {
      return null;
    }
    const id = element.id;
    if (id && !looksGeneratedId(id)) {
      const selector = `#${cssEscape(id)}`;
      if (isUniqueSelector(selector, root)) return selector;
    }
    for (const attribute of TEST_ATTRIBUTES) {
      const value = element.getAttribute(attribute);
      if (value) {
        const selector = `[${attribute}="${cssEscape(value)}"]`;
        if (isUniqueSelector(selector, root)) return selector;
      }
    }
    const tag = element.tagName.toLowerCase();
    if (["input", "select", "textarea", "button"].includes(tag)) {
      const name = element.getAttribute("name");
      if (name) {
        const selector = `${tag}[name="${cssEscape(name)}"]`;
        if (isUniqueSelector(selector, root)) return selector;
      }
    }
    if (tag === "html" || tag === "body") return tag;
    const segments = [];
    let current = element;
    let anchor = "";
    let depth = 0;
    while (current && current !== root && current.tagName && depth < 8) {
      const currentTag = current.tagName.toLowerCase();
      if (currentTag === "body" || currentTag === "html") break;
      const currentId = current.id;
      if (
        depth > 0 &&
        currentId &&
        !looksGeneratedId(currentId) &&
        isUniqueSelector(`#${cssEscape(currentId)}`, root)
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
    let selector = `${anchor ? `${anchor} > ` : ""}${segments.join(" > ")}`;
    if (selector && isUniqueSelector(selector, root)) return selector;
    while (current && current !== root && current.tagName) {
      const currentTag = current.tagName.toLowerCase();
      if (currentTag === "html") break;
      if (currentTag === "body") {
        segments.unshift("body");
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
    }
    selector = segments.join(" > ");
    return selector && isUniqueSelector(selector, root) ? selector : null;
  };
  const getDurableSelector = (element) => {
    if (!element || !element.getRootNode) return null;
    const root = element.getRootNode();
    const selector = selectorWithinRoot(element, root);
    if (!selector) return null;
    if (root && root.nodeType === 11 && root.host) {
      const hostSelector = getDurableSelector(root.host);
      return hostSelector ? `${hostSelector} ${selector}` : null;
    }
    return selector;
  };

  const SECRET_AUTOCOMPLETE_TOKENS = new Set([
    "current-password",
    "new-password",
    "one-time-code",
    "cc-name",
    "cc-number",
    "cc-csc",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
  ]);
  const CREDIT_CARD_HINT_PHRASES = [
    "card number",
    "card no",
    "credit card",
    "credit or debit card",
    "debit card",
    "security code",
    "cardholder",
    "cvv",
    "cvv2",
    "cvc",
  ];
  const TOTP_HINT_PHRASES = [
    "otp",
    "totp",
    "2fa",
    "two factor",
    "one time code",
    "verification code",
    "authenticator code",
    "pin",
    "recovery code",
    "login code",
    "access code",
  ];
  const PASSWORD_HINT_PHRASES = ["password", "passcode", "passphrase"];
  const SECRET_HINT_PHRASES = [
    "api key",
    "apikey",
    "api token",
    "access token",
    "refresh token",
    "auth token",
    "token",
    "access key",
    "secret key",
    "client secret",
    "webhook secret",
    "private key",
    "bearer token",
    "secret value",
    "secret",
    "routing number",
    "account number",
    "bank account",
    "acct",
    "social security number",
    "social security",
    "ssn",
    "passport",
    "tax id",
    "security answer",
    "seed phrase",
    "recovery phrase",
    "iban",
  ];

  const normalizeHaystack = (...parts) => {
    const joined = parts
      .filter(Boolean)
      .join(" ")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
      .toLowerCase();
    const normalized = joined
      .replace(/#/g, " number ")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
    return normalized ? ` ${normalized} ` : "";
  };

  const haystackHasPhrase = (haystack, phrase) => {
    if (!haystack) {
      return false;
    }
    const needle = ` ${String(phrase)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim()} `;
    return haystack.includes(needle);
  };

  const HIDDEN_INPUT_SELECTOR = 'input[type="hidden" i]';
  const FIELD_SELECTOR = 'input:not([type="hidden" i]),select,textarea';
  const LABEL_LIKE_SELECTOR =
    "label,span,p,div,strong,b,td,th,dt,legend,h1,h2,h3,h4,h5,h6";
  const ICON_SELECTOR =
    '[aria-hidden="true"],[class~="icon"],[class*="material-icons"],[class*="material-symbols"]';
  const MAX_LABEL_TEXT_LENGTH = 40;

  const isFieldGroup = (node) => {
    const children = Array.from(node.children).filter(
      (child) => !child.matches(HIDDEN_INPUT_SELECTOR),
    );
    return (
      children.length > 1 &&
      children.every((child) => child.matches(FIELD_SELECTOR))
    );
  };
  const holdsOneField = (node) =>
    node.querySelectorAll(FIELD_SELECTOR).length === 1;

  // Nearest short label-like text before the field, skipping buttons and icons; another field's row ends the walk.
  // A wrapper holding only fields (a card number split into boxes) takes the label above it.
  const HEADING_LIKE_SELECTOR = "h1,h2,h3,h4,h5,h6,legend,dt";
  const LABEL_BOUNDARY_SELECTOR = "form,section,fieldset,dialog";
  const MAX_HEADING_TEXT_LENGTH = 80;

  const hasText = (node) =>
    Boolean(node && String(node.textContent || "").trim());
  const labelTexts = (element) =>
    element.labels
      ? Array.from(element.labels, (label) => label.textContent)
      : [];
  const labelledByNodes = (element) => {
    const root = element.getRootNode && element.getRootNode();
    return String(
      (element.getAttribute && element.getAttribute("aria-labelledby")) || "",
    )
      .split(/\s+/)
      .filter(Boolean)
      .map(
        (id) =>
          (root && root.getElementById && root.getElementById(id)) ||
          (element.ownerDocument && element.ownerDocument.getElementById(id)),
      )
      .filter(Boolean);
  };
  const hasExplicitLabel = (element) =>
    Boolean(
      (element.labels && Array.from(element.labels).some(hasText)) ||
      (element.getAttribute &&
        String(element.getAttribute("aria-label") || "").trim()) ||
      labelledByNodes(element).some(hasText),
    );
  // The field's own label, sent to the model with a withheld value so the model can judge what the field was.
  const ownLabelText = (element) =>
    [
      ...labelTexts(element),
      element.getAttribute && element.getAttribute("aria-label"),
      ...labelledByNodes(element).map((node) => node.textContent),
      element.getAttribute && element.getAttribute("placeholder"),
      element.getAttribute && element.getAttribute("title"),
    ]
      .map((text) =>
        String(text || "")
          .replace(/\s+/g, " ")
          .trim(),
      )
      .find(Boolean)
      ?.slice(0, MAX_HEADING_TEXT_LENGTH) || "";

  const precedingLabelText = (element) => {
    // An email address is never a password, code or card number, so text near it decides nothing.
    if (String(element.type || "").toLowerCase() === "email") return "";
    const parentGroup =
      element.parentElement && isFieldGroup(element.parentElement);
    // A box in a split code or card group keeps the group's label even when it carries "Digit 2".
    if (hasExplicitLabel(element) && !parentGroup) return "";
    let current = element;
    for (let depth = 0; current && depth < 3; depth += 1) {
      const singleField = current === element || holdsOneField(current);
      if (
        current !== element &&
        current.matches(LABEL_BOUNDARY_SELECTOR) &&
        !singleField
      ) {
        return "";
      }
      const parent = current.parentElement;
      // A heading labels only a field, field group or single-field container below it; any other heading starts
      // a section the field is not part of.
      const headingAllowed =
        singleField || isFieldGroup(current) || current.matches("dd");
      if (!parent || !isFieldGroup(parent)) {
        for (
          let sibling = current.previousElementSibling;
          sibling;
          sibling = sibling.previousElementSibling
        ) {
          if (
            sibling.matches(FIELD_SELECTOR) ||
            sibling.querySelector(FIELD_SELECTOR)
          ) {
            return "";
          }
          const text = String(sibling.textContent || "").trim();
          if (sibling.matches(HEADING_LIKE_SELECTOR)) {
            return headingAllowed && text.length <= MAX_HEADING_TEXT_LENGTH
              ? text
              : "";
          }
          if (
            text.length <= MAX_LABEL_TEXT_LENGTH &&
            /[a-z]/i.test(text) &&
            sibling.matches(LABEL_LIKE_SELECTOR) &&
            !sibling.matches(ICON_SELECTOR)
          ) {
            return text;
          }
        }
        if (
          !parent ||
          Array.from(parent.querySelectorAll(FIELD_SELECTOR)).some(
            (field) => !current.contains(field),
          )
        ) {
          return "";
        }
      }
      current = parent;
    }
    return "";
  };

  const secretFieldHaystack = (element) =>
    normalizeHaystack(
      element.id,
      element.name,
      element.getAttribute && element.getAttribute("name"),
      element.getAttribute && element.getAttribute("aria-label"),
      element.getAttribute && element.getAttribute("placeholder"),
      element.getAttribute && element.getAttribute("title"),
      ...labelTexts(element),
      ...labelledByNodes(element).map((node) => node.textContent),
      precedingLabelText(element),
    );

  const inputTypeOf = (element) => String(element.type || "").toLowerCase();
  const autocompleteTokens = (element) =>
    String((element.getAttribute && element.getAttribute("autocomplete")) || "")
      .toLowerCase()
      .split(/\s+/);
  // A show-password toggle reveals into a text-like type; a remembered password moved to one of these is a
  // different field now.
  const NEVER_SECRET_INPUT_TYPES = new Set(["email", "search", "url"]);
  const isPasswordField = (element) => {
    if (!element) return false;
    if (inputTypeOf(element) === "password") return true;
    if (
      knownPasswords.has(element) &&
      !NEVER_SECRET_INPUT_TYPES.has(inputTypeOf(element))
    ) {
      return true;
    }
    return (
      autocompleteTokens(element).some((token) =>
        ["current-password", "new-password"].includes(token),
      ) ||
      PASSWORD_HINT_PHRASES.some((phrase) =>
        haystackHasPhrase(secretFieldHaystack(element), phrase),
      )
    );
  };

  const isHidden = (element) =>
    !element.getClientRects || element.getClientRects().length === 0;

  // A show-password toggle that reveals a separate text input: its value matches a password in the form, or it
  // sits alone beside a password input the person already filled and that is now hidden.
  const sharesCopiedPassword = (element) => {
    if (
      !element ||
      String(element.tagName || "").toLowerCase() !== "input" ||
      inputTypeOf(element) !== "text"
    ) {
      return false;
    }
    const value = String(element.value || "");
    const form = element.form || element.ownerDocument;
    const copiedFromPassword =
      value &&
      Array.from(form.querySelectorAll("input")).some(
        (candidate) =>
          candidate !== element &&
          isPasswordField(candidate) &&
          String(candidate.value || "") === value,
      );
    if (copiedFromPassword) return true;
    const parentInputs = element.parentElement
      ? Array.from(element.parentElement.querySelectorAll("input"))
      : [];
    return (
      parentInputs.length === 2 &&
      parentInputs.some(
        (candidate) =>
          candidate !== element &&
          isPasswordField(candidate) &&
          isHidden(candidate) &&
          secretElements.has(candidate) &&
          Boolean(candidate.value),
      )
    );
  };

  const NON_PASSWORD_SECRET_PHRASES = [
    ...TOTP_HINT_PHRASES,
    ...CREDIT_CARD_HINT_PHRASES,
    ...SECRET_HINT_PHRASES,
  ];

  // Whether to keep the value: leans toward withholding, so text near the field counts.
  const isSecretField = (element) => {
    if (!element) return false;
    if (isPasswordField(element) || sharesCopiedPassword(element)) return true;
    if (
      autocompleteTokens(element).some((token) =>
        SECRET_AUTOCOMPLETE_TOKENS.has(token),
      )
    ) {
      return true;
    }
    const haystack = secretFieldHaystack(element);
    return [...NON_PASSWORD_SECRET_PHRASES, ...PASSWORD_HINT_PHRASES].some(
      (phrase) => haystackHasPhrase(haystack, phrase),
    );
  };

  // What kind of field it is is a fact for the model and the credential card, so only the type and autocomplete
  // name one. Page text may veto "password" for a masked code box but never names a kind.
  const secretFieldKind = (element) => {
    if (!isSecretField(element)) return null;
    const tokens = autocompleteTokens(element);
    if (tokens.includes("one-time-code")) return "totp";
    if (tokens.some((token) => token.startsWith("cc-"))) return "other";
    if (inputTypeOf(element) !== "password") return "other";
    const haystack = secretFieldHaystack(element);
    return NON_PASSWORD_SECRET_PHRASES.some((phrase) =>
      haystackHasPhrase(haystack, phrase),
    )
      ? "other"
      : "password";
  };

  const ACTIONABLE =
    "a,button,input,select,textarea,label,summary,option,[role=button],[role=link]," +
    "[role=menuitem],[role=option],[role=tab],[role=checkbox],[role=radio],[role=switch],[onclick]";
  const TEXT_INPUT_TYPES = new Set([
    "",
    "text",
    "search",
    "email",
    "tel",
    "url",
    "password",
    "number",
    "date",
    "datetime-local",
    "month",
    "time",
    "week",
  ]);
  const UNSUPPORTED_VALUE_INPUT_TYPES = new Set(["range", "color"]);
  const CLICK_DRAG_THRESHOLD = 5;
  const KEY_NAMES = new Set([
    "Enter",
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageUp",
    "PageDown",
    "Home",
    "End",
  ]);
  const listeners = [];
  const secretElements = new WeakSet();
  const recordedSecretElements = new Set();
  const secretFieldSelectors = new Set();
  const secretFieldPositions = new Set();
  const announcedSecretFocus = new WeakSet();
  const queue = [];
  const stream = `${Date.now()}-${Math.random()}`;
  let seq = 0;
  let waiter = null;
  let holdTimer = null;
  let holding = false;
  let clickStart = null;
  let clickPointer = null;
  let pointerUp = false;
  let press = null;
  let nativeDrag = null;
  let suppressClick = false;
  let typing = null;
  let selectStart = null;
  let stopped = false;
  let installedDocument = document;
  let wakeQueued = false;

  const eventElement = (event) => {
    const first = event.composedPath && event.composedPath()[0];
    return first && first.nodeType === 1 ? first : event.target;
  };
  const clickElement = (element) =>
    element && element.closest
      ? element.closest(ACTIONABLE) || element
      : element;
  const fieldValue = (element) =>
    element.isContentEditable
      ? String(element.textContent || "")
      : String(element.value || "");
  const textField = (element) => {
    if (!element || element.nodeType !== 1) return null;
    if (element.isContentEditable)
      return element.closest("[contenteditable]") || element;
    const tag = (element.tagName || "").toLowerCase();
    if (tag === "textarea") return element;
    if (tag !== "input") return null;
    if (element.readOnly || element.disabled) return null;
    return TEXT_INPUT_TYPES.has(String(element.type || "").toLowerCase())
      ? element
      : null;
  };
  const unsupportedValueField = (element) => {
    if (!element || !element.tagName) return null;
    return element.tagName.toLowerCase() === "input" &&
      UNSUPPORTED_VALUE_INPUT_TYPES.has(
        String(element.type || "").toLowerCase(),
      )
      ? element
      : null;
  };
  const structuralPosition = (element) => {
    if (!element || !element.tagName) return "";
    const segments = [];
    let current = element;
    const root = element.getRootNode && element.getRootNode();
    while (current && current.tagName && current !== root) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      segments.unshift(
        `${current.tagName.toLowerCase()}:nth-of-type(${index})`,
      );
      current = current.parentElement;
    }
    return segments.join(" > ");
  };
  const nativeSelect = (element) => {
    if (!element || !element.tagName) return null;
    const tag = element.tagName.toLowerCase();
    if (tag === "select") return element;
    return tag === "option" && element.closest
      ? element.closest("select")
      : null;
  };
  const dropSnapshot = (element) => {
    let current = element;
    let start = null;
    while (current && !(start = snapshot(current)))
      current = current.parentElement;
    return start;
  };
  const emitDrag = (source, drop) => {
    if (!source || !drop) return false;
    // A drop on the source's own container is a move inside it, unless a sortable already reparented
    // the source into the destination mid-drag.
    const moved = source.element.parentElement !== source.origin;
    if (!moved && drop.element.contains(source.element)) return false;
    emit("drag", source, {
      target_selector: drop.selector,
      target_packet: drop.packet,
    });
    return true;
  };
  const emitDragGap = (start) => emit("freeform_drag", start || {});
  const selectionText = () =>
    String((globalThis.getSelection && globalThis.getSelection()) || "");
  const endPointerDrag = (event) => {
    const current = press;
    press = null;
    const source = current.element;
    // A stroke or slider gesture is never a text selection or an element-to-element drag, wherever it
    // is released.
    if (source.closest && source.closest("canvas,[role=slider]")) {
      suppressClick = true;
      emitDragGap(current.start);
      return;
    }
    // A drag that changed the text selection was selecting text; a highlight left from before is not.
    const selected = selectionText();
    if (selected && selected !== current.selected) return;
    const hits = document.elementsFromPoint(event.clientX, event.clientY);
    // ponytail: a drag library's floating copy of the source sits on top, so a same-tag, same-text hit is
    // skipped; an identical-looking sibling target is skipped too and records a gap instead of a drag.
    const hit = hits.find(
      (element) =>
        !source.contains(element) &&
        !(
          element.tagName === source.tagName &&
          element.textContent === source.textContent &&
          source.textContent.trim()
        ),
    );
    if (emitDrag(current.start, dropSnapshot(hit))) {
      suppressClick = true;
      return;
    }
    // A release still over the source is a jittery click and keeps its native click.
    if (!hits.length || source.contains(hits[0])) return;
    suppressClick = true;
    emitDragGap(current.start);
  };
  const labelTextField = (element) =>
    element && element.matches && element.matches("label")
      ? textField(element.control)
      : null;
  const packetFor = (element, selector) => {
    const packet = scoutPreActionPacket(element, selector, "", "", 5000);
    if (textField(element)) {
      packet.role_name = null;
      packet.role_name_match_count = -1;
    }
    if (element.getRootNode && element.getRootNode().nodeType === 11) {
      packet.selector_match_count = 1;
    }
    return packet;
  };
  const snapshot = (element) => {
    const selector = getDurableSelector(element);
    return selector
      ? {
          element,
          selector,
          packet: packetFor(element, selector),
          origin: element.parentElement,
        }
      : null;
  };
  const finishWaiter = (events) => {
    if (!waiter) return;
    const current = waiter;
    waiter = null;
    current(events);
  };
  const drained = () => ({ stream, events: queue.slice() });
  const empty = () => ({ stream, events: [] });
  const wakeNow = () => {
    if (waiter && !holding && queue.length) finishWaiter(drained());
  };
  const wake = () => {
    if (wakeQueued) return;
    wakeQueued = true;
    queueMicrotask(() => {
      wakeQueued = false;
      wakeNow();
    });
  };
  const beginHold = () => {
    holding = true;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(() => {
      holding = false;
      holdTimer = null;
      if (clickStart && pointerUp) {
        const start = clickStart;
        clickStart = null;
        clickPointer = null;
        pointerUp = false;
        emit("click", start);
      } else {
        wake();
      }
    }, 600);
  };
  const endHold = () => {
    holding = false;
    clearTimeout(holdTimer);
    holdTimer = null;
  };
  const cancelClick = () => {
    clickStart = null;
    clickPointer = null;
    pointerUp = false;
    endHold();
    wake();
  };
  const emit = (kind, start, extra = {}) => {
    if (!start) return;
    if (queue.length >= 1000) queue.shift();
    const payload = {
      kind,
      ts: Date.now(),
      url: location.href,
      seq: ++seq,
      ...extra,
    };
    if (start.selector) {
      payload.selector = start.selector;
      payload.packet = start.packet;
    }
    if (kind.startsWith("secret") && start.element) {
      recordedSecretElements.add(start.element);
    }
    queue.push(payload);
    wake();
  };
  const add = (target, type, listener) => {
    target.addEventListener(type, listener, true);
    listeners.push([target, type, listener]);
  };
  const startTyping = (element, initialValue = fieldValue(element)) => {
    const start = snapshot(element);
    if (!start) return;
    const field = secretFieldKind(element);
    const position = structuralPosition(element);
    const previouslySecret = secretFieldSelectors.has(start.selector);
    const secretByPosition = secretFieldPositions.has(position);
    if (field || previouslySecret || secretByPosition) {
      secretElements.add(element);
      secretFieldSelectors.add(start.selector);
      if (position) secretFieldPositions.add(position);
    }
    typing = {
      ...start,
      initialValue,
      secret: Boolean(field || previouslySecret || secretByPosition),
      field: field || "other",
    };
    if (
      (field || previouslySecret || secretByPosition) &&
      !announcedSecretFocus.has(element)
    ) {
      announcedSecretFocus.add(element);
      emit("secret_focus", typing, { field: field || "other" });
    }
  };
  const commitTyping = () => {
    if (!typing) return;
    const span = typing;
    typing = null;
    const value = fieldValue(span.element);
    if (span.initialValue !== null && value === span.initialValue) return;
    const field = secretFieldKind(span.element);
    if (field) {
      secretElements.add(span.element);
      secretFieldSelectors.add(span.selector);
      const position = structuralPosition(span.element);
      if (position) secretFieldPositions.add(position);
    }
    if (span.secret || secretElements.has(span.element) || field) {
      emit("secret", span, {
        typed_length: value.length,
        field: field || span.field,
        field_label: ownLabelText(span.element),
      });
    } else {
      emit("type", span, { value, typed_length: value.length });
    }
  };
  const fileInputFor = (element) => {
    if (!element) return null;
    if (
      element.tagName &&
      element.tagName.toLowerCase() === "input" &&
      String(element.type || "").toLowerCase() === "file"
    ) {
      return element;
    }
    if (!element.matches || !element.matches("label")) return null;
    if (
      element.control &&
      String(element.control.type || "").toLowerCase() === "file"
    )
      return element.control;
    const nested = element.querySelector('input[type="file"]');
    if (nested) return nested;
    const id = element.getAttribute("for");
    const control = id ? element.ownerDocument.getElementById(id) : null;
    return control && String(control.type || "").toLowerCase() === "file"
      ? control
      : null;
  };
  const keyName = (event) => {
    if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return null;
    if (
      event.key.length === 1 &&
      ((event.ctrlKey && event.altKey) || (!event.ctrlKey && !event.metaKey))
    )
      return null;
    const modifiers = [];
    if (event.ctrlKey) modifiers.push("Control");
    if (event.altKey) modifiers.push("Alt");
    if (event.shiftKey) modifiers.push("Shift");
    if (event.metaKey) modifiers.push("Meta");
    if (modifiers.length) return [...modifiers, event.key].join("+");
    return KEY_NAMES.has(event.key) ? event.key : null;
  };

  const watchedRoots = new WeakSet();
  const fieldsIn = (node) => {
    if (node.nodeType !== 1) return [];
    return node.matches(FIELD_SELECTOR)
      ? [node]
      : Array.from(node.querySelectorAll(FIELD_SELECTOR));
  };
  const rememberPasswords = (node) => {
    for (const field of fieldsIn(node)) {
      if (inputTypeOf(field) === "password") knownPasswords.add(field);
    }
  };
  // A show-password toggle can swap in a new input node (v-if/v-else, a re-rendered wrapper), sometimes after a
  // leave transition. The first lone field added under the container a lone password just left inherits it.
  const vacatedPasswordSlots = new Map();
  const VACATED_SLOT_MS = 1000;
  const passwordTypeObserver = new MutationObserver((mutations) => {
    const now = Date.now();
    const added = new Set();
    for (const mutation of mutations) {
      if (mutation.type === "attributes") {
        if (
          String(mutation.oldValue || "").toLowerCase() === "password" ||
          inputTypeOf(mutation.target) === "password"
        ) {
          knownPasswords.add(mutation.target);
        }
        continue;
      }
      for (const node of mutation.removedNodes) {
        const fields = fieldsIn(node);
        if (fields.length === 1 && isPasswordField(fields[0])) {
          vacatedPasswordSlots.set(mutation.target, now);
        }
      }
      for (const node of mutation.addedNodes) added.add(node);
    }
    for (const [slot, at] of vacatedPasswordSlots) {
      if (now - at > VACATED_SLOT_MS) vacatedPasswordSlots.delete(slot);
    }
    for (const node of added) {
      // A node inserted inside another node from this batch is covered by that node's subtree walk.
      if (added.has(node.parentNode)) continue;
      watchShadowRoots(node);
      rememberPasswords(node);
      const fields = fieldsIn(node);
      if (fields.length !== 1) continue;
      for (const slot of vacatedPasswordSlots.keys()) {
        if (slot.contains(fields[0])) {
          knownPasswords.add(fields[0]);
          vacatedPasswordSlots.delete(slot);
          break;
        }
      }
    }
  });
  // Shadow trees are invisible to a document observer, so each open root gets its own.
  const watchRoot = (root) => {
    if (watchedRoots.has(root)) return;
    watchedRoots.add(root);
    for (const field of root.querySelectorAll('input[type="password"]')) {
      knownPasswords.add(field);
    }
    passwordTypeObserver.observe(root, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["type"],
      attributeOldValue: true,
    });
    watchShadowRoots(root);
  };
  const watchShadowRoots = (node) => {
    if (!node || !node.querySelectorAll) return;
    if (node.shadowRoot) watchRoot(node.shadowRoot);
    for (const element of node.querySelectorAll("*")) {
      if (element.shadowRoot) watchRoot(element.shadowRoot);
    }
  };
  watchRoot(document);
  // A shadow root attached after its host was inserted raises no mutation, and a light-DOM button can toggle a
  // password inside it, so every click or Enter/Space looks for new open roots before the page handles it.
  const watchEventRoots = (event) => {
    for (const node of event.composedPath ? event.composedPath() : []) {
      const root = node.getRootNode && node.getRootNode();
      if (root && root !== document && root.host) watchRoot(root);
    }
    watchShadowRoots(document);
  };
  add(globalThis, "pointerdown", (event) => {
    if (!event.isTrusted) return;
    watchEventRoots(event);
    const raw = eventElement(event);
    const field = textField(raw);
    if (typing && !typing.element.contains(raw)) commitTyping();
    cancelClick();
    press = null;
    nativeDrag = null;
    suppressClick = false;
    if (event.button !== 0) return;
    const select = nativeSelect(raw);
    if (select) {
      selectStart = snapshot(select);
      return;
    }
    if (field || unsupportedValueField(raw)) return;
    const target = clickElement(raw);
    if (labelTextField(target)) return;
    clickStart = snapshot(target);
    press = {
      element: target,
      start: clickStart,
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      selected: selectionText(),
    };
    if (clickStart) {
      clickPointer = {
        id: event.pointerId,
        button: event.button,
        x: event.clientX,
        y: event.clientY,
      };
      beginHold();
    }
  });
  add(globalThis, "pointermove", (event) => {
    if (
      !event.isTrusted ||
      !clickPointer ||
      pointerUp ||
      event.pointerId !== clickPointer.id
    )
      return;
    if (
      Math.hypot(
        event.clientX - clickPointer.x,
        event.clientY - clickPointer.y,
      ) > CLICK_DRAG_THRESHOLD
    )
      cancelClick();
  });
  add(globalThis, "pointerup", (event) => {
    if (!event.isTrusted) return;
    if (
      press &&
      event.pointerId === press.id &&
      Math.hypot(event.clientX - press.x, event.clientY - press.y) >
        CLICK_DRAG_THRESHOLD
    )
      endPointerDrag(event);
    press = null;
    if (
      !clickPointer ||
      event.pointerId !== clickPointer.id ||
      event.button !== clickPointer.button
    )
      return;
    if (
      Math.hypot(
        event.clientX - clickPointer.x,
        event.clientY - clickPointer.y,
      ) > CLICK_DRAG_THRESHOLD
    ) {
      cancelClick();
      return;
    }
    pointerUp = true;
  });
  add(globalThis, "pointercancel", (event) => {
    if (!event.isTrusted) return;
    if (press && event.pointerId === press.id) press = null;
    if (clickPointer && event.pointerId === clickPointer.id) cancelClick();
  });
  add(globalThis, "dragstart", (event) => {
    if (!event.isTrusted) return;
    const raw = eventElement(event);
    nativeDrag = snapshot(
      (raw && raw.closest && raw.closest("[draggable=true]")) || raw,
    );
    press = null;
  });
  add(globalThis, "drop", (event) => {
    if (!event.isTrusted || !nativeDrag) return;
    const source = nativeDrag;
    nativeDrag = null;
    if (!emitDrag(source, dropSnapshot(eventElement(event))))
      emitDragGap(source);
  });
  add(globalThis, "dragend", (event) => {
    if (!event.isTrusted || !nativeDrag) return;
    const source = nativeDrag;
    nativeDrag = null;
    emitDragGap(source);
  });
  add(globalThis, "click", (event) => {
    if (!event.isTrusted) return;
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    if (event.detail === 0) return;
    const raw = eventElement(event);
    if (nativeSelect(raw)) return;
    if (unsupportedValueField(raw)) return;
    const target = clickElement(raw);
    if (textField(target) || labelTextField(target)) return;
    const start = clickStart || snapshot(target);
    clickStart = null;
    clickPointer = null;
    pointerUp = false;
    endHold();
    emit(fileInputFor(target) ? "file_click" : "click", start);
  });
  add(globalThis, "focusin", (event) => {
    if (!event.isTrusted) return;
    watchEventRoots(event);
    const target = eventElement(event);
    const field = textField(target);
    if (field) {
      startTyping(field);
      return;
    }
    if (target && target.tagName && target.tagName.toLowerCase() === "select") {
      selectStart = snapshot(target);
    }
  });
  add(globalThis, "focusout", (event) => {
    if (!event.isTrusted) return;
    if (typing && typing.element === textField(eventElement(event)))
      commitTyping();
  });
  const isSelect = (element) =>
    Boolean(
      element && element.tagName && element.tagName.toLowerCase() === "select",
    );
  const inShadowRoot = (element) =>
    Boolean(element.getRootNode && element.getRootNode() !== document);
  const emitSelect = (target) => {
    const option = target.selectedOptions && target.selectedOptions[0];
    const start =
      selectStart && selectStart.element === target
        ? selectStart
        : snapshot(target);
    const field = secretFieldKind(target);
    if (field)
      emit("secret_select", start, {
        field,
        field_label: ownLabelText(target),
      });
    else
      emit("select", start, {
        value: String(target.value || ""),
        label: option ? String(option.textContent || "").trim() : "",
      });
    selectStart = null;
  };
  add(globalThis, "change", (event) => {
    if (!event.isTrusted) return;
    const target = eventElement(event);
    if (isSelect(target)) {
      emitSelect(target);
      return;
    }
    const unsupportedValue = unsupportedValueField(target);
    if (unsupportedValue) {
      emit("unsupported_input", snapshot(unsupportedValue), {
        input_type: String(unsupportedValue.type || ""),
        value: fieldValue(unsupportedValue),
      });
      return;
    }
    // Date-like inputs fire change per completed segment; their span ends on focus loss or Enter instead.
    const segmented = /^(date|time|datetime-local|month|week)$/.test(
      String(target && target.type),
    );
    const focused =
      target &&
      target.getRootNode &&
      target.getRootNode().activeElement === target;
    if (
      typing &&
      typing.element === textField(target) &&
      !(segmented && focused)
    )
      commitTyping();
  });
  add(globalThis, "paste", (event) => {
    if (!event.isTrusted || typing) return;
    const field =
      textField(eventElement(event)) || textField(document.activeElement);
    if (field) startTyping(field);
  });
  add(globalThis, "input", (event) => {
    if (!event.isTrusted) return;
    const target = eventElement(event);
    // change is not composed, so a select inside a shadow root reports only through input.
    if (isSelect(target) && inShadowRoot(target)) {
      emitSelect(target);
      return;
    }
    if (typing) return;
    const field =
      textField(eventElement(event)) || textField(document.activeElement);
    if (field) startTyping(field, null);
  });
  add(globalThis, "keydown", (event) => {
    if (!event.isTrusted || event.repeat) return;
    if (event.key === "Enter" || event.key === " ") watchEventRoots(event);
    const raw = eventElement(event);
    const target = clickElement(raw);
    if (
      nativeSelect(raw) ||
      nativeSelect(target) ||
      nativeSelect(document.activeElement)
    )
      return;
    const field = textField(raw) || textField(document.activeElement);
    if (!typing && field && !event.isComposing) startTyping(field);
    const name = keyName(event);
    const namedKey =
      KEY_NAMES.has(event.key) &&
      !event.ctrlKey &&
      !event.altKey &&
      !event.metaKey;
    const inSecretField =
      Boolean(typing && typing.secret) ||
      isSecretField(raw) ||
      isSecretField(document.activeElement);
    const suppressKey = inSecretField && (!namedKey || event.key === "Tab");
    if (typing) {
      if (event.isComposing) return;
      const multiline =
        typing.element.tagName.toLowerCase() === "textarea" ||
        typing.element.isContentEditable;
      if (event.key === "Enter" && multiline) return;
      const commitsAndEmits =
        event.key === "Enter" ||
        event.key === "Tab" ||
        ["ArrowUp", "ArrowDown", "Escape", "PageUp", "PageDown"].includes(
          event.key,
        ) ||
        Boolean(name && (event.ctrlKey || event.altKey || event.metaKey));
      if (event.key === "Tab" || commitsAndEmits) commitTyping();
      if (suppressKey || !commitsAndEmits || !name) return;
      emit("key", snapshot(field || raw), { key: name });
      if (event.key === "Enter") wakeNow();
      return;
    }
    if (suppressKey) return;
    const tag = target && target.tagName && target.tagName.toLowerCase();
    const spaceControl =
      event.key === " " &&
      target &&
      target.matches &&
      target.matches("button,input[type=checkbox],input[type=radio]");
    if (name || spaceControl)
      emit("key", tag === "body" || tag === "html" ? {} : snapshot(target), {
        key: spaceControl ? "Space" : name,
      });
  });
  const flush = () => {
    commitTyping();
    const start = pointerUp ? clickStart : null;
    clickStart = null;
    clickPointer = null;
    pointerUp = false;
    endHold();
    if (start) emit("click", start);
    else wake();
  };
  add(globalThis, "pagehide", flush);

  const rearm = () => {
    const priorDocument = installedDocument;
    for (const listener of listeners) {
      const target = listener[0] === priorDocument ? document : listener[0];
      target.addEventListener(listener[1], listener[2], true);
      listener[0] = target;
    }
    installedDocument = document;
    api.document = document;
  };
  // The tap never reads a secret value, so nothing can scrub it later: empty those fields once the person is done,
  // before Copilot can read the live page.
  const clearSecretElement = (element) => {
    if (element.isContentEditable) {
      if (!element.textContent) return;
      element.textContent = "";
      element.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    if (String(element.tagName || "").toLowerCase() === "select") {
      if (element.selectedIndex === -1) return;
      element.selectedIndex = -1;
      element.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    if (!element.value) return;
    const descriptor = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(element),
      "value",
    );
    if (descriptor && descriptor.set) descriptor.set.call(element, "");
    else element.value = "";
    element.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const clearSecretValues = () => {
    const fields = new Set(recordedSecretElements);
    // A show-password toggle can copy the typed password into a text input the person never focused.
    const typedPasswords = new Set(
      Array.from(recordedSecretElements)
        .filter(isPasswordField)
        .map((field) => String(field.value || ""))
        .filter(Boolean),
    );
    if (typedPasswords.size) {
      for (const field of document.querySelectorAll("input,textarea")) {
        if (typedPasswords.has(String(field.value || ""))) fields.add(field);
      }
    }
    for (const field of fields) {
      try {
        clearSecretElement(field);
      } catch (_error) {
        // A detached or page-locked element keeps nothing worth retrying.
      }
    }
    recordedSecretElements.clear();
  };
  const api = {
    document,
    get stopped() {
      return stopped;
    },
    drain: (maxWaitMs, acknowledgedStream, acknowledgedSeq) =>
      new Promise((resolve) => {
        rearm();
        if (acknowledgedStream === stream) {
          while (queue.length && queue[0].seq <= acknowledgedSeq) queue.shift();
        }
        if (stopped) {
          resolve(empty());
          return;
        }
        if (queue.length && !holding) {
          resolve(drained());
          return;
        }
        if (waiter) waiter(empty());
        let settled = false;
        const timer = setTimeout(
          () => {
            if (settled) return;
            settled = true;
            if (waiter === finish) waiter = null;
            resolve(queue.length && !holding ? drained() : empty());
          },
          Math.max(0, Number(maxWaitMs) || 0),
        );
        const finish = (events) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(events);
        };
        waiter = finish;
      }),
    selectorFor: getDurableSelector,
    labelFor: ownLabelText,
    flush,
    stop: () => {
      if (stopped) return [];
      flush();
      stopped = true;
      clearSecretValues();
      passwordTypeObserver.disconnect();
      for (const [target, type, listener] of listeners) {
        target.removeEventListener(type, listener, true);
      }
      listeners.length = 0;
      finishWaiter(empty());
      return drained();
    },
  };
  globalThis.__skyvernDemoTap = api;
})();
