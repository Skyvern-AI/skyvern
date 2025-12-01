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
import { getRuntimeApiKey } from "@/util/env";
import { HiddenCopyableInput } from "@/components/ui/hidden-copyable-input";
import { OnePasswordTokenForm } from "@/components/OnePasswordTokenForm";
import { AzureClientSecretCredentialTokenForm } from "@/components/AzureClientSecretCredentialTokenForm";

function Settings() {
  const { environment, organization, setEnvironment, setOrganization } =
    useSettingsStore();
  const apiKey = getRuntimeApiKey();

  return (
    <div className="flex flex-col gap-8">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">설정</CardTitle>
          <CardDescription>
            환경 및 조직을 선택할 수 있습니다
          </CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-4">
              <Label className="w-36 whitespace-nowrap">환경</Label>
              <Select value={environment} onValueChange={setEnvironment}>
                <SelectTrigger>
                  <SelectValue placeholder="환경 선택" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="local">로컬</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-4">
              <Label className="w-36 whitespace-nowrap">조직</Label>
              <Select value={organization} onValueChange={setOrganization}>
                <SelectTrigger>
                  <SelectValue placeholder="조직 선택" />
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
          <CardTitle className="text-lg">API 키</CardTitle>
          <CardDescription>현재 사용 중인 API 키</CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <HiddenCopyableInput value={apiKey ?? "API 키를 찾을 수 없습니다"} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">1Password 연동</CardTitle>
          <CardDescription>
            1Password 서비스 계정 토큰을 관리합니다.{" "}
            <a
              href="https://developer.1password.com/docs/service-accounts/get-started/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 underline"
            >
              서비스 계정 생성 및 토큰 발급 방법 안내
            </a>
          </CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <OnePasswordTokenForm />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Azure 연동</CardTitle>
          <CardDescription>Azure 연동 설정을 관리합니다</CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          <AzureClientSecretCredentialTokenForm />
        </CardContent>
      </Card>
    </div>
  );
}

export { Settings };
