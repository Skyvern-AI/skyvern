/**
 * Progressively enhanced text copying with HTTP fallback
 * https://web.dev/patterns/clipboard/copy-text
 *
 * Uses navigator.clipboard when in a secure context (HTTPS),
 * falls back to textarea + execCommand for HTTP contexts.
 */
async function copyText(text: string): Promise<boolean> {
  // Prefer navigator.clipboard when in a secure context
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      // Fall through to fallback
    }
  }

  // Fallback for HTTP or when clipboard API fails
  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.style.position = "fixed";
  textArea.style.opacity = "0";
  textArea.style.left = "-9999px";
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();

  try {
    // execCommand is deprecated but remains the only option for HTTP contexts
    // where navigator.clipboard is unavailable. Browser support remains strong.
    const success = document.execCommand("copy");
    return success;
  } finally {
    document.body.removeChild(textArea);
  }
}

export { copyText };
