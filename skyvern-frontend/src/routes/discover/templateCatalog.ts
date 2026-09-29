type TemplateCategory = "gov" | "lookup" | "finance" | "purchasing" | "forms";

const TEMPLATE_CATEGORIES: Record<
  TemplateCategory,
  { label: string; filterLabel: string; stage: string; ink: string }
> = {
  gov: {
    label: "Government filing",
    filterLabel: "Government filings",
    stage: "bg-indigo-50 dark:bg-indigo-400/15",
    ink: "text-indigo-700 dark:text-indigo-300",
  },
  lookup: {
    label: "Lookup",
    filterLabel: "Lookups",
    stage: "bg-teal-50 dark:bg-teal-400/15",
    ink: "text-teal-700 dark:text-teal-300",
  },
  finance: {
    label: "Finance",
    filterLabel: "Finance",
    stage: "bg-amber-50 dark:bg-amber-400/15",
    ink: "text-amber-800 dark:text-amber-300",
  },
  purchasing: {
    label: "Purchasing",
    filterLabel: "Purchasing",
    stage: "bg-rose-50 dark:bg-rose-400/15",
    ink: "text-rose-700 dark:text-rose-300",
  },
  forms: {
    label: "Form",
    filterLabel: "Forms",
    stage: "bg-sky-50 dark:bg-sky-400/15",
    ink: "text-sky-700 dark:text-sky-300",
  },
};

const CATEGORY_ORDER: Array<TemplateCategory> = [
  "gov",
  "lookup",
  "finance",
  "purchasing",
  "forms",
];

// Most clicked first, per home.template_clicked in PostHog (Sept 2026). Each
// entry lists the same template's ids across production and staging.
const POPULAR_TEMPLATE_IDS: ReadonlyArray<ReadonlyArray<string>> = [
  ["wpid_462707285715519276", "wpid_369161749055928862"],
  ["wpid_459009284178448812"],
  ["wpid_459004551041605070"],
  ["wpid_459013964273286908"],
  ["wpid_459004674259025010"],
];

function categorizeTemplate(title: string): TemplateCategory | null {
  if (/\binvoices?\b/i.test(title)) return "finance";
  if (/\b(purchas\w*|checkout|orders?)\b/i.test(title)) return "purchasing";
  if (/\b(look\s?-?up|search|verif\w*)\b/i.test(title)) return "lookup";
  if (/\b(filings?|registration|annual report|renewal)\b/i.test(title))
    return "gov";
  if (/\b(application|contact|forms?)\b/i.test(title)) return "forms";
  return null;
}

/** Splits "Agency - Task" titles so the task can lead the card. */
function splitTemplateTitle(title: string): {
  name: string;
  publisher: string | null;
} {
  const match = /\s[-–—]\s/.exec(title);
  if (!match) return { name: title, publisher: null };
  const publisher = title.slice(0, match.index).trim();
  const name = title.slice(match.index + match[0].length).trim();
  if (!name || !publisher) return { name: title, publisher: null };
  return { name, publisher };
}

function popularityRank(workflowPermanentId: string): number {
  const rank = POPULAR_TEMPLATE_IDS.findIndex((ids) =>
    ids.includes(workflowPermanentId),
  );
  return rank === -1 ? POPULAR_TEMPLATE_IDS.length : rank;
}

export {
  CATEGORY_ORDER,
  TEMPLATE_CATEGORIES,
  categorizeTemplate,
  popularityRank,
  splitTemplateTitle,
};
export type { TemplateCategory };
