import { describe, expect, test } from "vitest";

import { rememberSopHandoffFile, takeSopHandoffFile } from "./sopHandoff";

describe("sopHandoff", () => {
  test("hands the file to its own workflow exactly once", () => {
    const file = new File(["%PDF"], "procedure.pdf");
    rememberSopHandoffFile("wpid_a", file);

    expect(takeSopHandoffFile("wpid_b")).toBeNull();
    expect(takeSopHandoffFile("wpid_a")).toBe(file);
    expect(takeSopHandoffFile("wpid_a")).toBeNull();
  });
});
