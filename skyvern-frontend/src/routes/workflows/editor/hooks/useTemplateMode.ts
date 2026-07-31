import { useState } from "react";
import { usePostHog } from "posthog-js/react";

import { useCurrentOrgId } from "@/hooks/useCurrentOrgId";
import { isTemplateExpression } from "@/util/googleSheetsUrl";

type Options = {
  value: string;
  event:
    | "sheets.spreadsheet.picker.template_mode_toggled"
    | "sheets.tab.template_mode_toggled";
  blockType: "google_sheets_read" | "google_sheets_write";
  onClear: () => void;
};

function useTemplateMode({ value, event, blockType, onClear }: Options) {
  const [templateMode, setTemplateMode] = useState(false);
  const postHog = usePostHog();
  const orgId = useCurrentOrgId();
  const pressed = templateMode || isTemplateExpression(value);

  const onChange = (enabled: boolean) => {
    setTemplateMode(enabled);
    if (enabled === false && isTemplateExpression(value)) {
      onClear();
    }
    postHog?.capture(event, {
      org_id: orgId,
      block_type: blockType,
      enabled,
    });
  };

  return { pressed, onChange };
}

export { useTemplateMode };
