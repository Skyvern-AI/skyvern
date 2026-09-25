import type { FailureCategory } from "@/api/types";

// Mirrors FailureCategory in skyvern/forge/failure_classifier.py. Workflow authors can also raise
// their own free-form error codes as the category; those are shown verbatim because customers match
// on the exact code, with the author's description (carried in `reasoning`) as the explanation.
type FailureCategoryDisplay = {
  label: string;
  description: string;
};

const FAILURE_CATEGORY_DISPLAY = {
  ANTI_BOT_DETECTION: {
    label: "Blocked by bot protection",
    description:
      "The site showed a CAPTCHA or bot check that the run couldn't get past.",
  },
  PROXY_ERROR: {
    label: "Proxy connection failed",
    description:
      "The run couldn't connect through its proxy. Retry, or try a different proxy location.",
  },
  BROWSER_ERROR: {
    label: "Browser error",
    description:
      "The browser crashed or the page closed unexpectedly. Retrying usually helps.",
  },
  NAVIGATION_FAILURE: {
    label: "Couldn't open the page",
    description:
      "The page didn't load or redirected somewhere unexpected, such as a 404. Check the URL.",
  },
  PAGE_LOAD_TIMEOUT: {
    label: "Page load timed out",
    description:
      "The page didn't finish loading in time. Check the URL, or retry if the site is slow.",
  },
  ELEMENT_STATE_TIMEOUT: {
    label: "Element never became ready",
    description:
      "An element on the page didn't become visible or clickable in time.",
  },
  AUTH_FAILURE: {
    label: "Login failed",
    description:
      "The run couldn't sign in to the site, for example because the login or a 2FA step was rejected.",
  },
  LLM_ERROR: {
    label: "AI model error",
    description:
      "The AI model running this task returned an error or didn't respond. This is usually temporary, so retry the run.",
  },
  CREDENTIAL_ERROR: {
    label: "Credential problem",
    description:
      "The credentials for this run are missing or couldn't be used. Check the credentials it uses.",
  },
  DATA_EXTRACTION_FAILURE: {
    label: "Couldn't extract data",
    description:
      "The run couldn't read or extract the requested data from the page. Check that the page loaded correctly and, if extracting, the goal and schema.",
  },
  ELEMENT_NOT_FOUND: {
    label: "Element not found",
    description: "The run couldn't find the element it needed on the page.",
  },
  WRONG_PAGE_STATE: {
    label: "Unexpected page",
    description:
      "The site showed a different page or state than the run expected.",
  },
  MAX_STEPS_EXCEEDED: {
    label: "Step limit reached",
    description:
      "The run used its maximum number of steps before finishing. Raise the step limit or simplify the goal.",
  },
  // Task V3 run caps: steps, turns, tool calls, tokens, or wall-clock time.
  BUDGET_EXHAUSTED: {
    label: "Step or time limit reached",
    description:
      "The run hit its step or time limit before finishing. This isn't a billing or credits issue. Raise the step limit or simplify the goal.",
  },
  LLM_REASONING_ERROR: {
    label: "AI chose the wrong action",
    description:
      "The AI misread the page or took the wrong action. A more specific goal often helps.",
  },
  INFRASTRUCTURE_ERROR: {
    label: "Temporary system issue",
    description:
      "Something went wrong on Skyvern's side, such as a network or capacity problem. Retrying usually helps.",
  },
  PARAMETER_BINDING_ERROR: {
    label: "Parameter setup failed",
    description:
      "An internal setup step for this run's parameters failed. Retry, or contact support if it persists.",
  },
  UNKNOWN: {
    label: "Unknown failure",
    description:
      "Skyvern couldn't determine the cause. See the failure reason for details.",
  },
} satisfies Record<string, FailureCategoryDisplay>;

type KnownFailureCategory = keyof typeof FAILURE_CATEGORY_DISPLAY;

function isKnownFailureCategory(
  category: string,
): category is KnownFailureCategory {
  return Object.prototype.hasOwnProperty.call(
    FAILURE_CATEGORY_DISPLAY,
    category,
  );
}

function getFailureCategoryDisplay({
  category,
  reasoning,
}: Pick<FailureCategory, "category" | "reasoning">): FailureCategoryDisplay {
  if (isKnownFailureCategory(category)) {
    return FAILURE_CATEGORY_DISPLAY[category];
  }
  return {
    label: category,
    description:
      (typeof reasoning === "string" && reasoning.trim()) ||
      "See the failure reason for details.",
  };
}

export { getFailureCategoryDisplay };
