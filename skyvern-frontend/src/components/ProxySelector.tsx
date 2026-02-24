import { ProxyLocation } from "@/api/types";
import {
  geoTargetToProxyLocationInput,
  proxyLocationToGeoTarget,
} from "@/util/geoData";
import { GeoTargetSelector } from "./GeoTargetSelector";

type Props = {
  value: ProxyLocation;
  onChange: (value: ProxyLocation) => void;
  className?: string;
  allowGranularSearch?: boolean;
  modalPopover?: boolean;
};

function ProxySelector({
  value,
  onChange,
  className,
  allowGranularSearch = true,
  modalPopover = false,
}: Props) {
  // Convert input (string enum or object) to GeoTarget for the selector
  const geoTargetValue = proxyLocationToGeoTarget(value);

  return (
    <GeoTargetSelector
      className={className}
      value={geoTargetValue}
      allowGranularSearch={allowGranularSearch}
      modalPopover={modalPopover}
      onChange={(newTarget) => {
        // Convert back to ProxyLocation enum if possible (for simple countries)
        // or keep as GeoTarget object
        const newValue = geoTargetToProxyLocationInput(newTarget);
        onChange(newValue);
      }}
    />
  );
}

export { ProxySelector };
