import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import type { CustomLLM, OrganizationApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCustomLLMs } from "@/hooks/useCustomLLMs";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/components/ui/use-toast";

const SKYVERN_DEFAULT_VALUE = "__SKYVERN_DEFAULT__";
const CUSTOM_LLM_PREFIX = "CUSTOM_LLM_";
const organizationsQueryKey = ["organizations"] as const;

type OrganizationDefaultsUpdate = {
  default_llm_key?: string;
  clear_default_llm_key?: true;
  default_secondary_llm_key?: string;
  clear_default_secondary_llm_key?: true;
};

type ModelSelectProps = Readonly<{
  id: string;
  label: string;
  description: string;
  value: string | null;
  customLLMs: Array<CustomLLM>;
  disabled: boolean;
  onValueChange: (value: string | null) => void;
}>;

function ModelSelect({
  id,
  label,
  description,
  value,
  customLLMs,
  disabled,
  onValueChange,
}: ModelSelectProps) {
  const customLLMKeys = useMemo(
    () =>
      new Set(
        customLLMs.map((customLLM) => `${CUSTOM_LLM_PREFIX}${customLLM.id}`),
      ),
    [customLLMs],
  );
  const deletedKey = value !== null && !customLLMKeys.has(value) ? value : null;

  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <p className="text-sm text-muted-foreground">{description}</p>
      <Select
        value={value ?? SKYVERN_DEFAULT_VALUE}
        onValueChange={(nextValue) =>
          onValueChange(nextValue === SKYVERN_DEFAULT_VALUE ? null : nextValue)
        }
        disabled={disabled}
      >
        <SelectTrigger id={id}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SKYVERN_DEFAULT_VALUE}>Skyvern Default</SelectItem>
          {deletedKey !== null && (
            <SelectItem value={deletedKey} disabled>
              {deletedKey} (deleted)
            </SelectItem>
          )}
          {customLLMs.map((customLLM) => (
            <SelectItem
              key={customLLM.id}
              value={`${CUSTOM_LLM_PREFIX}${customLLM.id}`}
            >
              {customLLM.config.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function OrgLLMDefaultsCard() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { customLLMs, isLoading: customLLMsAreLoading } = useCustomLLMs();
  const [smartDraft, setSmartDraft] = useState<string | null | undefined>(
    undefined,
  );
  const [fastDraft, setFastDraft] = useState<string | null | undefined>(
    undefined,
  );

  const organizationsQuery = useQuery<Array<OrganizationApiResponse>>({
    queryKey: organizationsQueryKey,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<{
        organizations: Array<OrganizationApiResponse>;
      }>("/organizations/");
      return response.data.organizations;
    },
  });
  const organization = organizationsQuery.data?.[0];
  const serverSmartLLMKey = organization?.default_llm_key ?? null;
  const serverFastLLMKey = organization?.default_secondary_llm_key ?? null;
  const smartLLMKey = smartDraft === undefined ? serverSmartLLMKey : smartDraft;
  const fastLLMKey = fastDraft === undefined ? serverFastLLMKey : fastDraft;

  const updateDefaultsMutation = useMutation({
    mutationFn: async (payload: OrganizationDefaultsUpdate) => {
      const client = await getClient(credentialGetter);
      await client.put("/organizations", payload);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: organizationsQueryKey });
      setSmartDraft(undefined);
      setFastDraft(undefined);
      toast({
        title: "Success",
        description: "Default models updated",
      });
    },
    onError: () => {
      toast({
        title: "Error",
        description: "Failed to update default models",
        variant: "destructive",
      });
    },
  });

  const hasChanges =
    organization !== undefined &&
    (smartLLMKey !== serverSmartLLMKey || fastLLMKey !== serverFastLLMKey);
  const controlsAreDisabled =
    organization === undefined ||
    organizationsQuery.isLoading ||
    customLLMsAreLoading ||
    updateDefaultsMutation.isPending;

  function saveDefaults() {
    if (!organization) {
      return;
    }

    const payload: OrganizationDefaultsUpdate = {};
    if (smartLLMKey !== serverSmartLLMKey) {
      if (smartLLMKey === null) {
        payload.clear_default_llm_key = true;
      } else {
        payload.default_llm_key = smartLLMKey;
      }
    }
    if (fastLLMKey !== serverFastLLMKey) {
      if (fastLLMKey === null) {
        payload.clear_default_secondary_llm_key = true;
      } else {
        payload.default_secondary_llm_key = fastLLMKey;
      }
    }

    updateDefaultsMutation.mutate(payload);
  }

  return (
    <Card>
      <CardHeader className="border-b-2">
        <CardTitle className="text-lg">Default Models</CardTitle>
        <CardDescription>
          Choose which custom models your organization uses by default.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 p-8">
        <ModelSelect
          id="smart-llm-default"
          label="Smart LLM"
          description="Used for planning and main reasoning."
          value={smartLLMKey}
          customLLMs={customLLMs}
          disabled={controlsAreDisabled}
          onValueChange={setSmartDraft}
        />
        <ModelSelect
          id="fast-llm-default"
          label="Fast LLM"
          description="Used for lightweight operations like validation, parsing, and mini-agents."
          value={fastLLMKey}
          customLLMs={customLLMs}
          disabled={controlsAreDisabled}
          onValueChange={setFastDraft}
        />
        <Button
          type="button"
          onClick={saveDefaults}
          disabled={!hasChanges || controlsAreDisabled}
        >
          {updateDefaultsMutation.isPending ? "Saving..." : "Save"}
        </Button>
      </CardContent>
    </Card>
  );
}

export { OrgLLMDefaultsCard };
