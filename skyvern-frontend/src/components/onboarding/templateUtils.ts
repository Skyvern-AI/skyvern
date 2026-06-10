import { ClickIcon } from "@/components/icons/ClickIcon";
import { ExtractIcon } from "@/components/icons/ExtractIcon";
import { SearchIcon } from "@/components/icons/SearchIcon";
import { GovernmentIcon } from "@/components/icons/GovernmentIcon";
import { BagIcon } from "@/components/icons/BagIcon";
import { ReceiptIcon } from "@/components/icons/ReceiptIcon";
import { DocumentIcon } from "@/components/icons/DocumentIcon";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

const INTENT_KEYWORDS: Record<string, RegExp> = {
  fill_forms:
    /form|application|submit|apply|registration|contact|intake|questionnaire|filing/i,
  extract_data:
    /extract|scrape|data|lookup|search|download|invoice|collect|report/i,
  monitor_website:
    /monitor|track|watch|check|alert|notification|change|verify/i,
};

function getTemplatesForIntent(
  templates: WorkflowApiResponse[],
  intent: string,
): WorkflowApiResponse[] {
  const pattern = INTENT_KEYWORDS[intent];
  if (!pattern) return templates.slice(0, 4);

  const matches = templates.filter((t) => {
    const text = `${t.title} ${t.description}`;
    return pattern.test(text);
  });

  if (matches.length >= 4) return matches.slice(0, 4);

  const matchedIds = new Set(matches.map((m) => m.workflow_permanent_id));
  const remaining = templates.filter(
    (t) => !matchedIds.has(t.workflow_permanent_id),
  );
  return [...matches, ...remaining].slice(0, 4);
}

function getTemplateIcon(
  template: WorkflowApiResponse,
): React.FC<{ className?: string }> {
  const text = `${template.title} ${template.description}`.toLowerCase();
  if (/form|application|submit|registration|contact/.test(text))
    return ClickIcon;
  if (/invoice|receipt|billing|payment/.test(text)) return ReceiptIcon;
  if (/extract|scrape|data|download/.test(text)) return ExtractIcon;
  if (/monitor|track|watch|check|alert/.test(text)) return SearchIcon;
  if (/government|entity|ein|sam|irs|federal/.test(text)) return GovernmentIcon;
  if (/job|career|hiring|recruit|employ/.test(text)) return BagIcon;
  return DocumentIcon;
}

function getSetupTime(template: WorkflowApiResponse): string {
  const blocks = template.workflow_definition.blocks.length;
  if (blocks <= 3) return "2 min";
  if (blocks <= 7) return "5 min";
  return "10 min";
}

export {
  INTENT_KEYWORDS,
  getTemplatesForIntent,
  getTemplateIcon,
  getSetupTime,
};
