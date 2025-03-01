import { ProxyLocation } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

type Props = {
  value: ProxyLocation | null;
  onChange: (value: ProxyLocation) => void;
  className?: string;
};

function ProxySelector({ value, onChange, className }: Props) {
  return (
    <Select value={value ?? ""} onValueChange={onChange}>
      <SelectTrigger className={className}>
        <SelectValue placeholder="Proxy Location" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ProxyLocation.Residential}>Residential</SelectItem>
        <SelectItem value={ProxyLocation.ResidentialISP}>
          Residential ISP (US)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialES}>
          Residential (Spain)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialIE}>
          Residential (Ireland)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialIN}>
          Residential (India)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialJP}>
          Residential (Japan)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialGB}>
          Residential (United Kingdom)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialFR}>
          Residential (France)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialDE}>
          Residential (Germany)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialNZ}>
          Residential (New Zealand)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialZA}>
          Residential (South Africa)
        </SelectItem>
        <SelectItem value={ProxyLocation.ResidentialAR}>
          Residential (Argentina)
        </SelectItem>
      </SelectContent>
    </Select>
  );
}

export { ProxySelector };
