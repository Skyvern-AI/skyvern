/**
 * Progressively enhanced text copying
 * https://web.dev/patterns/clipboard/copy-text
 */
async function copyText(text: string): Promise<void> {
  if ("clipboard" in navigator) {
    return navigator.clipboard.writeText(text);
  } else {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.opacity = "0";
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    const success = document.execCommand("copy");
    document.body.removeChild(textArea);
    if (success) {
      return Promise.resolve();
    } else {
      return Promise.reject();
    }
  }
}

export { copyText };
