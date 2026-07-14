// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { LightningBoltIcon } from "@radix-ui/react-icons";
import { afterEach, describe, expect, it } from "vitest";

import { TriggerType } from "@/api/types";
import { TriggerTypeBadge } from "./TriggerTypeBadge";

afterEach(cleanup);

function iconMarkup(root: HTMLElement): string {
  return root.querySelector("svg")?.innerHTML ?? "";
}

describe("TriggerTypeBadge", () => {
  it("does not reuse the cached lightning-bolt glyph for API runs", () => {
    // The gold lightning bolt is the "ran with code" (cached) indicator on
    // /runs. The API trigger badge must use a distinct glyph so an API run is
    // never mistaken for a cached run. (SKY-12158)
    const { container: apiBadge } = render(
      <TriggerTypeBadge triggerType={TriggerType.Api} />,
    );
    const { container: bolt } = render(<LightningBoltIcon />);

    expect(iconMarkup(apiBadge)).not.toBe("");
    expect(iconMarkup(apiBadge)).not.toBe(iconMarkup(bolt));
  });

  it("renders a distinct glyph for each trigger type", () => {
    const glyphs = [
      TriggerType.Manual,
      TriggerType.Mcp,
      TriggerType.Api,
      TriggerType.Scheduled,
    ].map((triggerType) => {
      const { container } = render(
        <TriggerTypeBadge triggerType={triggerType} />,
      );
      return iconMarkup(container);
    });

    expect(new Set(glyphs).size).toBe(glyphs.length);
  });
});
