import {
  ERROR_CODES,
  ProtocolError,
  requireArgs,
  requireTabId,
} from "./protocol.js";

function assertWebTab(tab) {
  const url = typeof tab?.url === "string" ? tab.url.trim().toLowerCase() : "";
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    throw new ProtocolError(
      ERROR_CODES.RESTRICTED_URL,
      "This operation is available only on HTTP or HTTPS tabs.",
    );
  }
}

export async function evaluateDom() {
  throw new ProtocolError(
    ERROR_CODES.OP_NOT_ALLOWED,
    "Direct JavaScript evaluation is unavailable in extension mode. Use skyvern_observe, skyvern_get_html, skyvern_find, or skyvern_get_value to inspect the page. Use skyvern_click or skyvern_type to interact.",
  );
}

const FORM_FILL_ERRORS = Object.freeze({
  document_changed:
    "The selected document changed or the fill expired. Inspect the page before retrying.",
  authorization_lost:
    "The fixed fill is no longer authorized for this tab. Inspect its sharing state.",
  password_field:
    "Password and credential fields require the stored-credential login tool.",
  invalid_selector: "Fixed form filling requires a valid CSS selector.",
  missing_target:
    "The selector did not match an input in the selected document.",
  ambiguous_target:
    "The selector matched more than one element. Use a unique CSS selector.",
  unsupported_target:
    "Fixed form filling supports only text inputs and textareas.",
  invalid_text:
    "The input would sanitize this text. Remove unsupported newlines or surrounding whitespace.",
  not_editable: "The selected input is hidden, disabled, or read-only.",
  value_not_retained:
    "The page did not retain the requested value. Inspect the page before retrying.",
});

const pendingFormFills = new Map();

export async function authorizeDomFill(tabScope, message, sender) {
  const denied = { authorized: false };
  if (
    message === null ||
    typeof message !== "object" ||
    Array.isArray(message) ||
    Object.keys(message).some(
      (key) => !["type", "requestId", "documentToken", "phase"].includes(key),
    ) ||
    message.type !== "skyvern.formFillAuthorize" ||
    typeof message.requestId !== "string" ||
    !["inspect", "commit"].includes(message.phase)
  )
    return denied;
  const pending = pendingFormFills.get(message.requestId);
  if (
    pending === undefined ||
    !pending.ready ||
    pending.authorized ||
    (message.phase === "commit") !== (pending.inspected === true) ||
    sender?.id !== chrome.runtime.id ||
    sender.tab?.id !== pending.tabId ||
    sender.frameId !== 0 ||
    typeof sender.documentId !== "string" ||
    sender.url !== pending.url ||
    message.documentToken !== pending.documentToken
  )
    return denied;
  try {
    pending.lease.assertCurrent();
    const tab = await tabScope.assertControllableLocked(
      pending.tabId,
      pending.lease,
    );
    pending.lease.assertCurrent();
    if (
      pendingFormFills.get(message.requestId) !== pending ||
      pending.authorized ||
      (message.phase === "commit") !== (pending.inspected === true) ||
      Date.now() >= pending.expiresAt ||
      (tab.pendingUrl || tab.url) !== pending.url
    )
      return denied;
    if (message.phase === "inspect") pending.inspected = true;
    else pending.authorized = true;
    return { authorized: true };
  } catch {
    return denied;
  }
}

export function revokeDomFills(tabId) {
  for (const [requestId, pending] of pendingFormFills) {
    if (pending.tabId === tabId) {
      pendingFormFills.delete(requestId);
      pending.lease.cancel(
        new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "Chrome ended control of this tab.",
        ),
      );
    }
  }
}

export async function fillDomInput(tabScope, args) {
  const values = requireArgs(args);
  if (
    Object.keys(values).some(
      (key) => !["tabId", "selector", "text", "deadline"].includes(key),
    ) ||
    typeof values.selector !== "string" ||
    values.selector.length === 0 ||
    values.selector.length > 4096 ||
    typeof values.text !== "string" ||
    values.text.length > 65536 ||
    !Number.isSafeInteger(values.deadline) ||
    values.deadline > Date.now() + 30000
  ) {
    throw new ProtocolError(
      ERROR_CODES.OP_NOT_ALLOWED,
      "A fixed form fill requires a CSS selector, plain text, and a deadline.",
    );
  }
  const tabId = requireTabId(values.tabId);
  const requestId = crypto.randomUUID();
  const pending = { tabId, ready: false, authorized: false };
  let deadlineTimer;
  try {
    return await tabScope.runTabOperation(
      tabId,
      async (lease) => {
        if (Date.now() >= values.deadline) {
          throw new ProtocolError(
            ERROR_CODES.COMMAND_TIMEOUT,
            FORM_FILL_ERRORS.document_changed,
          );
        }
        const tab = await tabScope.assertControllableLocked(tabId, lease);
        assertWebTab(tab);
        const url = tab.pendingUrl || tab.url;
        try {
          const probe = await chrome.tabs.sendMessage(
            tabId,
            { type: "skyvern.formFill", action: "probe" },
            { frameId: 0 },
          );
          lease.assertCurrent();
          const currentTab = await tabScope.assertControllableLocked(
            tabId,
            lease,
          );
          assertWebTab(currentTab);
          if (
            probe?.ok !== true ||
            typeof probe.documentToken !== "string" ||
            probe.documentToken.length === 0 ||
            probe.documentToken.length > 128 ||
            probe.url !== url ||
            (currentTab.pendingUrl || currentTab.url) !== url ||
            Date.now() >= values.deadline
          ) {
            throw new ProtocolError(
              ERROR_CODES.COMMAND_TIMEOUT,
              FORM_FILL_ERRORS.document_changed,
            );
          }
          lease.assertCurrent();
          const expiresAt = Math.min(values.deadline, Date.now() + 1000);
          Object.assign(pending, {
            url,
            documentToken: probe.documentToken,
            expiresAt,
            ready: true,
          });
          const response = await chrome.tabs.sendMessage(
            tabId,
            {
              type: "skyvern.formFill",
              action: "fill",
              requestId,
              documentToken: probe.documentToken,
              url,
              expiresAt,
              selector: values.selector,
              text: values.text,
            },
            { frameId: 0 },
          );
          lease.assertCurrent();
          assertWebTab(await tabScope.assertControllableLocked(tabId, lease));
          if (
            response?.ok !== true ||
            response.textLength !== values.text.length
          ) {
            const message = Object.hasOwn(FORM_FILL_ERRORS, response?.error)
              ? FORM_FILL_ERRORS[response.error]
              : "The fixed form fill was not confirmed. Inspect the page before retrying.";
            throw new ProtocolError(ERROR_CODES.CDP_ERROR, message);
          }
          return { textLength: response.textLength };
        } catch (error) {
          lease.assertCurrent();
          await tabScope.assertControllableLocked(tabId, lease);
          if (error instanceof ProtocolError) throw error;
          throw new ProtocolError(
            ERROR_CODES.OP_NOT_ALLOWED,
            "Fixed form controls are unavailable in this document. Reload the tab after updating the extension.",
          );
        }
      },
      undefined,
      true,
      (lease) => {
        pending.lease = lease;
        pendingFormFills.set(requestId, pending);
        deadlineTimer = setTimeout(
          () =>
            lease.cancel(
              new ProtocolError(
                ERROR_CODES.COMMAND_TIMEOUT,
                FORM_FILL_ERRORS.document_changed,
              ),
            ),
          Math.max(0, values.deadline - Date.now()),
        );
      },
    );
  } finally {
    clearTimeout(deadlineTimer);
    pendingFormFills.delete(requestId);
  }
}
