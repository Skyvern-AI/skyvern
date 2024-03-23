import { Label } from "@/components/ui/label";
import { useId } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSettingsStore } from "@/store/SettingsStore";

function Settings() {
  const { environment, organization, setEnvironment, setOrganization } =
    useSettingsStore();
  const environmentInputId = useId();
  const organizationInputId = useId();

  return (
    <div className="flex flex-col gap-6">
      <h1>Settings</h1>
      <div className="flex flex-col gap-4">
        <Label htmlFor={environmentInputId}>Environment</Label>
        <Select value={environment} onValueChange={setEnvironment}>
          <SelectTrigger className="w-full">
            <SelectValue placeholder="Environment" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="local">local</SelectItem>
          </SelectContent>
        </Select>
        <Label htmlFor={organizationInputId}>Organization</Label>
        <Select value={organization} onValueChange={setOrganization}>
          <SelectTrigger className="w-full">
            <SelectValue placeholder="Organization" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="skyvern">Skyvern</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

export { Settings };
