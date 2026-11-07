import { describe, expect, test } from "vitest";

import { failureHeadlineInPlainWords } from "./docFailureText";
import { docOneActionStepWantsTask } from "./docOneAction";

const headline = (raw: string) => raw.split(". ")[0]!;

describe("a step's failure in plain words", () => {
  test("the retry limit says how many tries were made and drops page element ids", () => {
    const raw =
      "Max retries per step (3) exceeded. Possible failure reasons: The actions failed because the agent tried to type into the search button instead of a text input. The page does have a search entry point, but AAAP itself does not support text input.";
    const text = failureHeadlineInPlainWords(raw, headline);
    expect(text.startsWith("Gave up after 3 tries: The actions failed")).toBe(
      true,
    );
    expect(text).not.toContain("that element itself");
    expect(text).not.toContain("AAAP");
    expect(text).not.toContain("Max retries");
  });

  test("a wrapped retry failure keeps its reason, and a ticker is not an element id", () => {
    expect(
      failureHeadlineInPlainWords(
        "navigation block failed. failure reason: Max retries per step (3) exceeded. Possible failure reasons: website down",
        headline,
      ),
    ).toBe("Gave up after 3 tries: website down");
    expect(
      failureHeadlineInPlainWords("Could not find AAPL quote", headline),
    ).toBe("Could not find AAPL quote");
  });

  test("a step run with no page to start on says so without internal names", () => {
    const text = failureHeadlineInPlainWords(
      "navigation block failed. failure reason: block 'go_to_url_2' has no starting URL set. The first browser block must have a URL to navigate to. Set a URL on the block, or reference a workflow parameter (e.g. '{{ starting_url }}').",
      headline,
    );
    expect(text).toMatch(/needs a page to start on/);
    expect(text).not.toMatch(/go_to_url_2|block|workflow parameter|\{\{/);
  });

  test("other failures keep their own headline", () => {
    expect(failureHeadlineInPlainWords("Page timed out. More.", headline)).toBe(
      "Page timed out",
    );
  });
});

describe("a Browser Action whose instruction needs several actions", () => {
  const action = (goal: string) =>
    ({ id: "a", type: "action", data: { navigationGoal: goal } }) as never;

  test("two actions joined by 'and' still count", () => {
    expect(
      docOneActionStepWantsTask(action("Click Submit and refresh the page")),
    ).toBe(true);
  });

  test("a plain 'and' joining two objects of one action does not count", () => {
    for (const goal of [
      "Enter username and password",
      "Read the title and price",
    ]) {
      expect(docOneActionStepWantsTask(action(goal))).toBe(false);
    }
  });

  test("is offered a Browser Task, a one-action instruction is not", () => {
    expect(
      docOneActionStepWantsTask(
        action("Search for apple stock and get its price"),
      ),
    ).toBe(true);
    expect(docOneActionStepWantsTask(action("Click the search button"))).toBe(
      false,
    );
    expect(docOneActionStepWantsTask(action("{{ first and second }}"))).toBe(
      false,
    );
    expect(
      docOneActionStepWantsTask(
        action("Search for apple stock and then open the first result"),
      ),
    ).toBe(true);
    expect(
      docOneActionStepWantsTask({
        id: "n",
        type: "navigation",
        data: { navigationGoal: "Search and read the price" },
      } as never),
    ).toBe(false);
  });
});
