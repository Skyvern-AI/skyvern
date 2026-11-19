import { describe, expect, test } from "vitest";

import type { AppNode } from "../nodes";
import { docStepCaption } from "./docStepRegistry";
import { docNameLabelPreview, docNameToLabel } from "./docStepNameLabel";

const titled = (label: string) =>
  ({
    id: "n",
    type: "navigation",
    position: { x: 0, y: 0 },
    data: { label },
  }) as AppNode;

describe("docNameToLabel", () => {
  test.each([
    ["Fill the form", "Fill_the_form"],
    ["  Sample login  ", "Sample_login"],
    ["Login (HR)", "Login_HR"],
    ["Café menu", "Cafe_menu"],
    ["a - b", "a_b"],
    ["already_a_label", "already_a_label"],
    ["2nd pass", "step_2nd_pass"],
    ["!!!", ""],
    ["", ""],
  ])("%s saves as %s", (name, label) => {
    expect(docNameToLabel(name)).toBe(label);
  });

  test.each(["Fill the form", "Read the invoice total", "Sample login"])(
    "%s reads back as the same title",
    (name) => {
      expect(docStepCaption(titled(docNameToLabel(name)))).toBe(name);
    },
  );
});

describe("docNameLabelPreview", () => {
  test("names the saved label only when it reads differently from the typed name", () => {
    expect(docNameLabelPreview("Login (HR)")).toBe("Login_HR");
    expect(docNameLabelPreview("Café menu")).toBe("Cafe_menu");
    expect(docNameLabelPreview("Fill the form")).toBeNull();
    expect(docNameLabelPreview("!!!")).toBeNull();
  });
});
