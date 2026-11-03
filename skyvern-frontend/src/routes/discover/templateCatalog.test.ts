import { describe, expect, it } from "vitest";
import { categorizeTemplate, splitTemplateTitle } from "./templateCatalog";

describe("templateCatalog", () => {
  it.each([
    ["Invoice Downloading", "finance"],
    [
      "California Secretary of State - Statement of Information CA and Out-of-State Filing",
      "gov",
    ],
    ["California Employment Development Department (EDD) - DE1 Filing", "gov"],
    ["Delaware Division of Corporations - Entity Lookup", "lookup"],
    ["Behavior Analyst Certification Board (BACB) - Name Lookup", "lookup"],
    ["Purchasing", "purchasing"],
    ["County Recorder - Document Search", "lookup"],
    ["Job Application Workflow [Staging]", "forms"],
    ["Delaware - Certificate of Formation", null],
  ])("categorizes %s as %s", (title, category) => {
    expect(categorizeTemplate(title)).toBe(category);
  });

  it("leads with the task and keeps the agency as the publisher", () => {
    expect(
      splitTemplateTitle("Internal Revenue Service (IRS) - SS-4 Filing"),
    ).toEqual({
      name: "SS-4 Filing",
      publisher: "Internal Revenue Service (IRS)",
    });
    expect(splitTemplateTitle("Invoice Downloading")).toEqual({
      name: "Invoice Downloading",
      publisher: null,
    });
    expect(splitTemplateTitle("Trailing - ")).toEqual({
      name: "Trailing - ",
      publisher: null,
    });
  });
});
