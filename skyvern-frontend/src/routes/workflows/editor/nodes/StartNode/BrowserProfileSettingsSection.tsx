import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useBrowserProfileQuery } from "@/routes/browserProfiles/hooks/useBrowserProfileQuery";
import { useBrowserProfileUsageQuery } from "@/routes/browserProfiles/hooks/useBrowserProfileUsageQuery";

import {
  BrowserProfileControl,
  ProfileModeToggle,
} from "@/routes/workflows/components/BrowserProfileControl";
import {
  autoCaption,
  classifyProfileRole,
  controlStateFromFields,
  credentialPinnedIpCaption,
  pickCaption,
  rotationWarning,
  sharedWriteWarning,
  virtualOwnProfileLabel,
} from "@/routes/workflows/components/browserProfileControlModel";
import { useBrowserProfileScenario } from "@/routes/workflows/hooks/useBrowserProfileScenario";

import { type WorkflowStartNodeData } from "./types";

const PIN_IP_TOOLTIP =
  "Pin this agent's saved sessions to a consistent proxy IP across runs, so restored logins are not invalidated by IP changes. Requires the Residential (ISP) proxy location.";

const WARNING_CHIP =
  "rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-500";

function BrowserProfileSettingsSection({
  blockId,
  data,
  update,
  agentName,
}: {
  blockId: string;
  data: WorkflowStartNodeData;
  update: (patch: Partial<WorkflowStartNodeData>) => void;
  agentName?: string;
}) {
  const { workflowPermanentId } = useParams();
  const { scn, credName, credentialCount, credentialPinned } =
    useBrowserProfileScenario();

  const derivedMode = controlStateFromFields({
    browserProfileId: data.browserProfileId,
    browserProfileKey: data.browserProfileKey,
  }).mode;
  const [mode, setMode] = useState(derivedMode);
  useEffect(() => {
    // Re-sync only when a stored field actually dictates a mode (external reload /
    // version switch). When both fields are empty — e.g. the user cleared the key
    // text mid-edit — keep the current mode so "Per input" isn't ejected.
    if (data.browserProfileId || data.browserProfileKey) {
      setMode(derivedMode);
    }
  }, [derivedMode, data.browserProfileId, data.browserProfileKey]);

  const handleModeChange = (next: "dropdown" | "code") => {
    setMode(next);
    // Per-input keys only resolve inside the backend's persist branch
    // (service.py: browser_profile_key applies in no-pick + persist rows), so
    // entering per-input must enable persistence for the profiles to be kept.
    update(
      next === "dropdown"
        ? { browserProfileKey: null }
        : { browserProfileId: null, persistBrowserSession: true },
    );
  };

  const perInput = mode === "code";

  const { data: pickedProfile } = useBrowserProfileQuery(
    data.browserProfileId ?? undefined,
  );
  const pickedRole = pickedProfile ? classifyProfileRole(pickedProfile) : null;
  const credentialOwnedPick = pickedRole === "credential";

  // F5: a legacy persist-ON workflow with no materialized pick shows its own
  // memory as a derived pick — never an empty "None" that lies about the state.
  const virtualOwnPick =
    !data.browserProfileId && !perInput && !!data.persistBrowserSession;
  const restingLabel =
    virtualOwnPick && agentName ? virtualOwnProfileLabel(agentName) : "None";
  const restingCaption = virtualOwnPick
    ? pickCaption("plain")
    : autoCaption(scn, credName);

  // F4a: a picked profile that other agents also use — shared-living warning.
  const { data: usage } = useBrowserProfileUsageQuery(
    data.browserProfileId ?? undefined,
    { enabled: Boolean(data.browserProfileId) },
  );
  const otherAgent = (usage?.workflows ?? []).find(
    (w) => w.workflow_permanent_id !== workflowPermanentId,
  );
  const sharedWarning =
    data.browserProfileId && !credentialOwnedPick && otherAgent
      ? sharedWriteWarning(otherAgent.title)
      : null;

  // F4b: rotating credentials but a single picked profile — accounts overwrite.
  const rotationWarn =
    scn === "rotation" && data.browserProfileId && !credentialOwnedPick
      ? rotationWarning(credentialCount)
      : null;

  // F6: own-pin applies to a profile this agent writes. Credential-owned picks are
  // credential-managed (hide). When the login credential is pinned and the pick
  // isn't, the run uses the credential's pin — show that read-only instead.
  const maintainsProfile =
    !!data.browserProfileId || perInput || !!data.persistBrowserSession;
  const showCredPinnedCaption =
    maintainsProfile && !credentialOwnedPick && credentialPinned && !!credName;
  const showOwnPin =
    maintainsProfile && !credentialOwnedPick && !showCredPinnedCaption;

  return (
    <div className="space-y-4 text-left">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <Label>Browser profile</Label>
          <ProfileModeToggle
            active={perInput}
            onToggle={() => handleModeChange(perInput ? "dropdown" : "code")}
          />
        </div>
        <BrowserProfileControl
          mode={mode}
          profileId={data.browserProfileId}
          onProfileChange={(id) =>
            update({
              browserProfileId: id,
              browserProfileKey: null,
              // Picking "None" on a legacy persist-ON agent means off — clear it.
              ...(id === null ? { persistBrowserSession: false } : {}),
            })
          }
          codeValue={data.browserProfileKey ?? ""}
          onCodeChange={(value) =>
            update({ browserProfileKey: value || null, browserProfileId: null })
          }
          codeMode="expression"
          restingCaption={restingCaption}
          restingLabel={restingLabel}
          nodeId={blockId}
        />
      </div>

      {sharedWarning && <div className={WARNING_CHIP}>⚠︎ {sharedWarning}</div>}
      {rotationWarn && <div className={WARNING_CHIP}>⚠︎ {rotationWarn}</div>}

      {showCredPinnedCaption && (
        <div className="border-t border-border/50 pt-4">
          <p className="text-xs text-muted-foreground">
            {credentialPinnedIpCaption(credName ?? "the credential")}
          </p>
        </div>
      )}

      {showOwnPin && (
        <div className="border-t border-border/50 pt-4">
          <div className="flex items-start gap-4">
            <div className="flex-1 space-y-1">
              <div className="flex items-center gap-2">
                <Label>Keep the same IP across runs</Label>
                <HelpTooltip content={PIN_IP_TOOLTIP} />
              </div>
              <p className="text-xs text-muted-foreground">
                Some sites sign you out when the network changes. This reserves
                a dedicated IP for this agent’s runs so the saved session stays
                valid.
              </p>
            </div>
            <Switch
              checked={data.pinSavedSessionIp}
              onCheckedChange={(value) => update({ pinSavedSessionIp: value })}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export { BrowserProfileSettingsSection };
