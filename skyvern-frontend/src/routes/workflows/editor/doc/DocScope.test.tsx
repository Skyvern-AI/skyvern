// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { DocScope } from "./DocScope";

describe("DocScope", () => {
  test("shows a credential-in-note problem next to the saved note", () => {
    render(
      <DocScope
        items={[
          {
            kind: "note",
            ownerNodeId: "step-1",
            placement: "above",
            text: "Use {{ login }} to sign in",
          },
        ]}
        byId={new Map()}
        noteProblems={
          new Map([
            [
              "step-1:above",
              {
                code: "credential_in_note",
                message: "Notes can't use a credential input.",
              },
            ],
          ])
        }
      />,
    );

    expect(document.querySelector('[title="{{ login }}"]')?.textContent).toBe(
      "login",
    );
    expect(
      screen.getByText("Notes can't use a credential input."),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-doc-note-problem="credential_in_note"]'),
    ).toBeTruthy();
  });
});
