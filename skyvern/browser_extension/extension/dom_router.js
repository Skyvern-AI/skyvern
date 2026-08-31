import {
  ERROR_CODES,
  ProtocolError,
  requireArgs,
  requireTabId,
} from "./protocol.js";

const MAX_EXPRESSION_LENGTH = 1_000_000;

function assertWebTab(tab) {
  const url = typeof tab?.url === "string" ? tab.url.trim().toLowerCase() : "";
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    throw new ProtocolError(
      ERROR_CODES.RESTRICTED_URL,
      "DOM evaluation is available only on HTTP or HTTPS tabs.",
    );
  }
}

function pageEvaluationError(injectionResult) {
  if (
    typeof injectionResult?.error !== "string" ||
    injectionResult.error.length === 0
  ) {
    return null;
  }
  return new ProtocolError(
    ERROR_CODES.CDP_ERROR,
    `The page expression failed: ${injectionResult.error.slice(0, 500)}`,
  );
}

export async function evaluateDom(tabScope, args) {
  const values = requireArgs(args);
  if (
    Object.keys(values).some((key) => !["tabId", "expression"].includes(key)) ||
    typeof values.expression !== "string" ||
    values.expression.length === 0 ||
    values.expression.length > MAX_EXPRESSION_LENGTH
  ) {
    throw new ProtocolError(
      ERROR_CODES.OP_NOT_ALLOWED,
      "A DOM evaluation requires a valid expression.",
    );
  }
  const tabId = requireTabId(values.tabId);
  return tabScope.runTabOperation(tabId, async (lease) => {
    assertWebTab(await tabScope.assertControllableLocked(tabId, lease));
    let injectionResults;
    try {
      injectionResults = await chrome.userScripts.execute({
        target: { tabId },
        world: "MAIN",
        injectImmediately: true,
        js: [{ code: values.expression }],
      });
    } catch {
      lease.assertCurrent();
      assertWebTab(await tabScope.assertControllableLocked(tabId, lease));
      throw new ProtocolError(
        ERROR_CODES.CDP_ERROR,
        "Chrome could not run this page expression. Enable Allow User Scripts for Skyvern Agent and retry.",
      );
    }
    lease.assertCurrent();
    assertWebTab(await tabScope.assertControllableLocked(tabId, lease));
    const injectionResult = injectionResults?.[0];
    const evaluationError = pageEvaluationError(injectionResult);
    if (evaluationError !== null) {
      throw evaluationError;
    }
    return { result: injectionResult?.result ?? null };
  });
}
