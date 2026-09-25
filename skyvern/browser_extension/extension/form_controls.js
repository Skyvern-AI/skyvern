// This passive, isolated-world listener implements one fixed operation. It does
// not accept source code or run until this extension's background worker asks.
(() => {
  const MESSAGE_TYPE = "skyvern.formFill";
  const documentToken = Array.from(
    crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  const textInputTypes = new Set(["text", "email", "search", "tel", "url"]);
  const credentialName =
    /password|passwd|passphrase|secret|(?:^|[-_])(?:otp|totp|token)(?:$|[-_])/i;
  const failure = (error) => ({ ok: false, error });

  function targetError(element) {
    const input = element instanceof HTMLInputElement;
    const textarea = element instanceof HTMLTextAreaElement;
    const autocomplete = element.getAttribute("autocomplete") || "";
    if (
      (input && element.type === "password") ||
      /current-password|new-password|one-time-code|\bcc-/i.test(autocomplete) ||
      ["name", "id", "aria-label"].some((key) =>
        credentialName.test(element.getAttribute(key) || ""),
      )
    )
      return "password_field";
    if (!textarea && (!input || !textInputTypes.has(element.type)))
      return "unsupported_target";
    if (
      !element.isConnected ||
      element.matches(":disabled") ||
      element.readOnly ||
      element.getAttribute("aria-disabled") === "true" ||
      element.getAttribute("aria-readonly") === "true" ||
      element.closest(
        "[inert], [aria-disabled='true'], [aria-readonly='true']",
      ) !== null ||
      !Array.from(element.getClientRects()).some(
        (rect) => rect.width > 0 && rect.height > 0,
      ) ||
      !element.checkVisibility({
        opacityProperty: true,
        visibilityProperty: true,
        contentVisibilityAuto: true,
      })
    )
      return "not_editable";
    return null;
  }

  async function handleMessage(message) {
    if (message.action === "probe") {
      if (
        Object.keys(message).some((key) => !["type", "action"].includes(key))
      ) {
        return failure("invalid_request");
      }
      return { ok: true, documentToken, url: location.href };
    }
    const keys = [
      "type",
      "action",
      "requestId",
      "documentToken",
      "url",
      "expiresAt",
      "selector",
      "text",
    ];
    if (
      message.action !== "fill" ||
      typeof message.requestId !== "string" ||
      message.requestId.length === 0 ||
      message.requestId.length > 128 ||
      Object.keys(message).some((key) => !keys.includes(key)) ||
      typeof message.selector !== "string" ||
      message.selector.length === 0 ||
      message.selector.length > 4096 ||
      typeof message.text !== "string" ||
      message.text.length > 65536
    ) {
      return failure("invalid_request");
    }
    if (
      message.documentToken !== documentToken ||
      message.url !== location.href ||
      !Number.isFinite(message.expiresAt) ||
      message.expiresAt <= Date.now() ||
      message.expiresAt > Date.now() + 1000
    ) {
      return failure("document_changed");
    }
    const authorization = await chrome.runtime.sendMessage({
      type: "skyvern.formFillAuthorize",
      requestId: message.requestId,
      documentToken,
      phase: "inspect",
    });
    if (
      authorization?.ok !== true ||
      authorization.result?.authorized !== true
    ) {
      return failure("authorization_lost");
    }
    if (message.url !== location.href || Date.now() >= message.expiresAt) {
      return failure("document_changed");
    }
    let elements;
    try {
      elements = document.querySelectorAll(message.selector);
    } catch {
      return failure("invalid_selector");
    }
    if (elements.length !== 1) {
      return failure(elements.length ? "ambiguous_target" : "missing_target");
    }
    const element = elements[0];
    const input = element instanceof HTMLInputElement;
    const textarea = element instanceof HTMLTextAreaElement;
    const initialError = targetError(element);
    if (initialError) return failure(initialError);
    const initialType = input ? element.type : "textarea";
    const initialMultiple = input && element.multiple;
    const expectedText = textarea
      ? message.text.replace(/\r\n?/g, "\n")
      : message.text;
    // Inputs sanitize newlines (and email/URL whitespace). Reject such text
    // before touching the live field or dispatching any events.
    if (input) {
      // Do not clone the live element: cloning a customized built-in can run
      // its page-defined constructor, including focus or other side effects.
      const probe = document.createElement("input");
      probe.type = initialType;
      probe.multiple = initialMultiple;
      probe.value = message.text;
      if (probe.value !== message.text) return failure("invalid_text");
    }
    const commitAuthorization = await chrome.runtime.sendMessage({
      type: "skyvern.formFillAuthorize",
      requestId: message.requestId,
      documentToken,
      phase: "commit",
    });
    if (
      commitAuthorization?.ok !== true ||
      commitAuthorization.result?.authorized !== true
    ) {
      return failure("authorization_lost");
    }
    // No asynchronous work follows the final grant. Revalidate any state that
    // could have changed while awaiting it; revocation cannot undo a committed write.
    const currentElements = document.querySelectorAll(message.selector);
    if (
      currentElements.length !== 1 ||
      currentElements[0] !== element ||
      (input &&
        (element.type !== initialType || element.multiple !== initialMultiple))
    )
      return failure("document_changed");
    const finalError = targetError(element);
    if (finalError) return failure(finalError);
    if (message.url !== location.href || Date.now() >= message.expiresAt) {
      return failure("document_changed");
    }
    const prototype = input
      ? HTMLInputElement.prototype
      : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value").set;
    setter.call(element, expectedText);
    element.dispatchEvent(
      new Event("input", { bubbles: true, composed: true }),
    );
    element.dispatchEvent(new Event("change", { bubbles: true }));
    if (!element.isConnected || element.value !== expectedText) {
      return failure("value_not_retained");
    }
    return { ok: true, textLength: message.text.length };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (
      sender.id !== chrome.runtime.id ||
      sender.tab !== undefined ||
      message === null ||
      typeof message !== "object" ||
      Array.isArray(message) ||
      message.type !== MESSAGE_TYPE ||
      window.top !== window ||
      !["http:", "https:"].includes(location.protocol)
    ) {
      return;
    }
    void handleMessage(message)
      .then(sendResponse)
      .catch(() => sendResponse(failure("operation_failed")));
    return true;
  });
})();
