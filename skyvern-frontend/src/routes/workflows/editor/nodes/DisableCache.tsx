import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

import { helpTooltips } from "../helpContent";

function DisableCache({
  cacheActions,
  disableCache,
  editable,
  // --
  onCacheActionsChange,
  onDisableCacheChange,
}: {
  cacheActions?: boolean;
  disableCache: boolean;
  editable: boolean;
  // --
  onCacheActionsChange: (cacheActions: boolean) => void;
  onDisableCacheChange: (disableCache: boolean) => void;
}) {
  return (
    <>
      {/* NOTE: Cache Actions is deprecated, and will be removed 
        
        It has been explicitly requested to only show this when 'cache actions' is `true`
        for the block. If it's `false`, we are not showing it.
      
      */}
      {cacheActions && (
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">
              Cache Actions
            </Label>
            <HelpTooltip content={helpTooltips["extraction"]["cacheActions"]} />
          </div>
          <div className="w-52">
            <Switch
              checked={cacheActions}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                onCacheActionsChange(checked);
              }}
            />
          </div>
        </div>
      )}
      <div className="flex items-center justify-between">
        <div className="flex gap-2">
          <Label className="text-xs font-normal text-slate-300">
            Disable Cache
          </Label>
          <HelpTooltip content="Disable caching for this block." />
        </div>
        <div className="w-52">
          <Switch
            checked={disableCache}
            onCheckedChange={(checked) => {
              if (!editable) {
                return;
              }
              onDisableCacheChange(checked);
            }}
          />
        </div>
      </div>
    </>
  );
}

export { DisableCache };
