import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSettingsStore } from "@/store/SettingsStore";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

function Settings() {
  const { environment, organization, setEnvironment, setOrganization } =
    useSettingsStore();

  return (
    <div className="flex flex-col gap-8">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Settings</CardTitle>
          <CardDescription>
            You can select environment and organization here
          </CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <div className="flex flex-col gap-4">
            <div className="flex gap-4 items-center">
              <Label className="whitespace-nowrap w-36">Environment</Label>
              <Select value={environment} onValueChange={setEnvironment}>
                <SelectTrigger>
                  <SelectValue placeholder="Environment" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="local">local</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex gap-4 items-center">
              <Label className="whitespace-nowrap w-36">Organization</Label>
              <Select value={organization} onValueChange={setOrganization}>
                <SelectTrigger>
                  <SelectValue placeholder="Organization" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="skyvern">Skyvern</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export { Settings };
