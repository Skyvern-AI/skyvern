import { useEffect, useState } from "react";

const PLACEHOLDERS = [
  "Go to a supplier portal, log in, and download last month's invoices as PDF",
  "Search a job board for Solutions Engineer roles in Toronto and apply to the first three",
  "Pull every row out of the vendor price list and return it as JSON",
  "Check the competitor's pricing page each morning and tell me when it changes",
  "Log in to the claims portal, submit the form, and save the confirmation number",
];
const RESTING = "Enter your prompt...";

// Types out example prompts as the placeholder until the user engages; then rests.
function useCyclingPlaceholder(active: boolean): string {
  const [text, setText] = useState(RESTING);

  useEffect(() => {
    if (!active) {
      setText(RESTING);
      return;
    }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setText(PLACEHOLDERS[0]!);
      return;
    }
    let index = 0;
    let cursor = 0;
    let deleting = false;
    let timer = window.setTimeout(tick, 700);
    function tick() {
      const full = PLACEHOLDERS[index]!;
      if (!deleting) {
        cursor += 1;
        setText(`${full.slice(0, cursor)}|`);
        if (cursor < full.length) {
          timer = window.setTimeout(tick, 34);
        } else {
          deleting = true;
          timer = window.setTimeout(tick, 1500);
        }
      } else {
        cursor -= 1;
        setText(`${full.slice(0, cursor)}|`);
        if (cursor > 0) {
          timer = window.setTimeout(tick, 14);
        } else {
          deleting = false;
          index = (index + 1) % PLACEHOLDERS.length;
          timer = window.setTimeout(tick, 320);
        }
      }
    }
    return () => window.clearTimeout(timer);
  }, [active]);

  return text;
}

export { useCyclingPlaceholder };
