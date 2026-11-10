import { describe, expect, test } from "vitest";
import type { WorkflowBlock } from "../../types/workflowTypes";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import type { DocGraph } from "./docGraphEdits";
import { docPromptUrlOffer, docShortUrl } from "./docPromptUrl";
import { block, settings } from "./docTestBlocks";

const step = (type: string, data: Record<string, unknown>) =>
  ({ id: "a", type, position: { x: 0, y: 0 }, data }) as AppNode;

describe("docPromptUrlOffer", () => {
  test.each([
    [
      "one URL",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/page and search",
      }),
      "https://example.com/page",
    ],
    [
      "a trailing period",
      step("task", {
        url: "",
        navigationGoal: "Search on https://example.com/page.",
      }),
      "https://example.com/page",
    ],
    [
      "two URLs",
      step("task", {
        url: "",
        navigationGoal: "Open https://example.com/a then https://example.com/b",
      }),
      null,
    ],
    [
      "the same URL twice",
      step("extraction", {
        url: " ",
        dataExtractionGoal:
          "From https://example.com/a (https://example.com/a), get the price",
      }),
      "https://example.com/a",
    ],
    [
      "a templated URL",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/{{ id }}",
      }),
      null,
    ],
    [
      "a host without a scheme",
      step("task", { url: "", navigationGoal: "Go to example.com/page" }),
      null,
    ],
    [
      "a URL already set",
      step("task", {
        url: "https://example.com/other",
        navigationGoal: "Go to https://example.com/page",
      }),
      null,
    ],
    [
      "a closing bracket the URL did not open",
      step("task", {
        url: "",
        navigationGoal: "Search the site (https://example.com/page)!",
      }),
      "https://example.com/page",
    ],
    [
      "brackets the URL opened",
      step("task", {
        url: "",
        navigationGoal: "Open https://example.com/a_(b) and search",
      }),
      "https://example.com/a_(b)",
    ],
    [
      "emphasis around the URL",
      step("task", {
        url: "",
        navigationGoal: "Open *https://example.com/a*?",
      }),
      "https://example.com/a",
    ],
    [
      "a host the URL field rejects",
      step("task", { url: "", navigationGoal: "Go to http://1.2.3/a" }),
      null,
    ],
    [
      "an intranet host the URL field accepts",
      step("task", { url: "", navigationGoal: "Go to http://intranet/app" }),
      "http://intranet/app",
    ],
    [
      "localhost",
      step("task", {
        url: "",
        navigationGoal: "Go to http://localhost:3000/a",
      }),
      "http://localhost:3000/a",
    ],
    [
      "an IP address",
      step("task", { url: "", navigationGoal: "Go to http://10.0.0.1/a" }),
      "http://10.0.0.1/a",
    ],
    [
      "a template tag",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/{%if x%}a{%endif%}",
      }),
      null,
    ],
    [
      "one URL written two ways",
      step("task", {
        url: "",
        navigationGoal: "Open https://Example.com then https://example.com/",
      }),
      "https://Example.com",
    ],
    [
      "a URL only in Extract",
      step("task", {
        url: "",
        navigationGoal: " ",
        dataExtractionGoal: "Get the price from https://example.com/page",
      }),
      null,
    ],
    [
      "a URL only in Complete if",
      step("task", {
        url: "",
        navigationGoal: "Search for shoes",
        completeCriterion: "Done when on https://example.com/thanks",
      }),
      null,
    ],
    [
      "a URL in the goal, another in Extract",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/goal",
        dataExtractionGoal: "Get the price from https://example.com/other",
      }),
      "https://example.com/goal",
    ],
    [
      "smart quotes around the URL",
      step("task", {
        url: "",
        navigationGoal:
          "Go to \u201chttps://example.com/catalog\u201d and search",
      }),
      "https://example.com/catalog",
    ],
    [
      "a bare host in smart quotes",
      step("task", {
        url: "",
        navigationGoal: "Open \u2018https://example.com\u2019",
      }),
      "https://example.com",
    ],
    [
      "a full-width comma, then CJK text",
      step("task", {
        url: "",
        navigationGoal:
          "\u6253\u5f00https://example.com/a\uff0c\u7136\u540e\u641c\u7d22",
      }),
      "https://example.com/a",
    ],
    [
      "a trailing ellipsis",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/a\u2026",
      }),
      "https://example.com/a",
    ],
    [
      "a trailing zero-width space",
      step("task", {
        url: "",
        navigationGoal: "Go to https://example.com/a\u200b",
      }),
      "https://example.com/a",
    ],
    [
      "ASCII quotes around the URL",
      step("task", {
        url: "",
        navigationGoal: 'Go to "https://example.com/a" and search',
      }),
      "https://example.com/a",
    ],
    [
      "a type without a URL field",
      step("textPrompt", { prompt: "Summarize https://example.com/page" }),
      null,
    ],
    [
      "a task v2 step, which opens its prompt's URL itself",
      step("taskv2", { url: "", prompt: "Search https://example.com/page" }),
      null,
    ],
    [
      "a 2.0 navigation step, which leaves no page",
      step("navigation", {
        url: "",
        prompt: "Search https://example.com/page",
        engine: "skyvern-2.0",
      }),
      null,
    ],
  ])("%s", (_case, node, expected) => {
    expect(
      docPromptUrlOffer({ nodes: [node], edges: [] }, node)?.url ?? null,
    ).toBe(expected);
  });

  // A quadratic trim runs for tens of seconds at this size, a linear one for milliseconds.
  test(
    "a long run of closing brackets after a URL is trimmed",
    { timeout: 5_000 },
    () => {
      const node = step("task", {
        url: "",
        navigationGoal: `Go to https://example.com/a${")".repeat(200_000)}`,
      });
      expect(docPromptUrlOffer({ nodes: [node], edges: [] }, node)?.url).toBe(
        "https://example.com/a",
      );
    },
  );
});

const goal = (label: string, next: string | null, navigation_goal = "") =>
  block("task", label, next, { url: "", navigation_goal });

// The run check decides the wording: the first page-opening step needs the URL, a later one would start on the previous step's page.
describe("docPromptUrlOffer in run order", () => {
  test.each<[string, WorkflowBlock[], boolean | null]>([
    [
      "the first browser step",
      [
        block("text_prompt", "summary", "target", { prompt: "Hi" }),
        goal("target", null, "Go to https://example.com/a"),
      ],
      true,
    ],
    [
      "a step after a login",
      [
        block("login", "sign_in", "target", {
          url: "https://example.com/login",
          navigation_goal: "Log in",
        }),
        goal("target", null, "Go to https://example.com/a"),
      ],
      false,
    ],
    [
      "a third step with the URL only in its prompt",
      [
        goal("first", "second", "Go to https://example.com/start"),
        goal("second", "target"),
        goal(
          "target",
          null,
          "Open https://example.com/cart and read the total",
        ),
      ],
      false,
    ],
  ])("%s", (_case, blocks, required) => {
    const graph = getElements(blocks, settings(), true) as DocGraph;
    const target = graph.nodes.find(
      (n) => isWorkflowBlockNode(n) && n.data.label === "target",
    )!;
    expect(docPromptUrlOffer(graph, target)?.required ?? null).toBe(required);
  });
});

test("docShortUrl keeps the scheme and host and shortens the path", () => {
  expect(docShortUrl("https://example.com/")).toBe("https://example.com");
  expect(
    docShortUrl("http://accounts.example.com.evil.example.net/login?next=/a"),
  ).toBe("http://accounts.example.com.evil.example.net/…");
  const short = docShortUrl(
    "https://example.com/a/very/long/path/to/some/page?query=1",
  );
  expect(short.startsWith("https://example.com/a/very/")).toBe(true);
  expect(short.endsWith("…")).toBe(true);
});
