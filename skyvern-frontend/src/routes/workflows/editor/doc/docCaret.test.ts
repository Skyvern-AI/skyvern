// @vitest-environment jsdom
import { expect, test } from "vitest";

import { placeCaretAt, splitAtCaret } from "./docCaret";

test("the caret returns to the character offset it had, not the end", () => {
  const line = document.body.appendChild(document.createElement("span"));
  line.append("Open ", document.createElement("br"), "the page now");

  placeCaretAt(line, 9);

  expect(splitAtCaret(line)).toEqual(["Open \nthe", " page now"]);
  line.remove();
});
