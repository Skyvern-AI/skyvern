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
import { envCredential } from "@/util/env";
import { HiddenCopyableInput } from "@/components/ui/hidden-copyable-input";
import { OnePasswordTokenForm } from "@/components/OnePasswordTokenForm";

function Settings() {
  const { environment, organization, setEnvironment, setOrganization } =
    useSettingsStore();
  const apiKey = envCredential;

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
            <div className="flex items-center gap-4">
              <Label className="w-36 whitespace-nowrap">Environment</Label>
              <Select value={environment} onValueChange={setEnvironment}>
                <SelectTrigger>
                  <SelectValue placeholder="Environment" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="local">local</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-4">
              <Label className="w-36 whitespace-nowrap">Organization</Label>
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
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">API Key</CardTitle>
          <CardDescription>Currently active API key</CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <HiddenCopyableInput value={apiKey ?? "API key not found"} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">1Password Integration</CardTitle>
          <CardDescription>
            Manage your 1Password service account token.{" "}
            <a
              href="https://developer.1password.com/docs/service-accounts/get-started/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 underline"
            >
              Learn how to create a service account and get your token.
            </a>
          </CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <OnePasswordTokenForm />
        </CardContent>
      </Card>
    </div>
  );
}

export { Settings };
