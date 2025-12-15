import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { CardStackIcon, PlusIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "./useCredentialModalState";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialsList } from "./CredentialsList";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { KeyIcon } from "@/components/icons/KeyIcon";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CredentialsTotpTab } from "./CredentialsTotpTab";
import { useSearchParams } from "react-router-dom";

const subHeaderText =
  "비밀번호, 신용카드를 안전하게 저장하고 워크플로우에서 사용할 2FA 코드를 관리하세요.";

const TAB_VALUES = ["passwords", "creditCards", "twoFactor"] as const;
type TabValue = (typeof TAB_VALUES)[number];
const DEFAULT_TAB: TabValue = "passwords";

function CredentialsPage() {
  const { setIsOpen, setType } = useCredentialModalState();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const matchedTab = TAB_VALUES.find((tab) => tab === tabParam);
  const activeTab: TabValue = matchedTab ?? DEFAULT_TAB;

  useEffect(() => {
    if (tabParam && !matchedTab) {
      const params = new URLSearchParams(searchParams);
      params.set("tab", DEFAULT_TAB);
      setSearchParams(params, { replace: true });
    }
  }, [tabParam, matchedTab, searchParams, setSearchParams]);

  function handleTabChange(value: string) {
    const nextTab = TAB_VALUES.find((tab) => tab === value) ?? DEFAULT_TAB;
    const params = new URLSearchParams(searchParams);
    params.set("tab", nextTab);
    setSearchParams(params, { replace: true });
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl">인증 정보</h1>
      <div className="flex items-center justify-between">
        <div className="w-96 text-sm text-slate-300">{subHeaderText}</div>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button>
              <PlusIcon className="mr-2 size-6" /> 추가
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-48">
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.PASSWORD);
              }}
              className="cursor-pointer"
            >
              <KeyIcon className="mr-2 size-4" />
              비밀번호
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.CREDIT_CARD);
              }}
              className="cursor-pointer"
            >
              <CardStackIcon className="mr-2 size-4" />
              신용카드
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <Tabs
        value={activeTab}
        className="space-y-4"
        onValueChange={handleTabChange}
      >
        <TabsList className="bg-slate-elevation1">
          <TabsTrigger value="passwords">비밀번호</TabsTrigger>
          <TabsTrigger value="creditCards">신용카드</TabsTrigger>
          <TabsTrigger value="twoFactor">2단계 인증</TabsTrigger>
        </TabsList>

        <TabsContent value="passwords" className="space-y-4">
          <CredentialsList filter="password" />
        </TabsContent>

        <TabsContent value="creditCards" className="space-y-4">
          <CredentialsList filter="credit_card" />
        </TabsContent>

        <TabsContent value="twoFactor" className="space-y-4">
          <CredentialsTotpTab />
        </TabsContent>
      </Tabs>
      <CredentialsModal />

      {/* Footer note - only for Passwords and Credit Cards tabs */}
      {activeTab !== "twoFactor" && (
        <div className="mt-8 border-t border-slate-700 pt-4">
          <div className="text-sm italic text-slate-400">
            <strong>참고:</strong> 이 기능을 사용하려면 Bitwarden 호환 서버({" "}
            <a
              href="https://bitwarden.com/help/self-host-an-organization/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              자체 호스팅 Bitwarden
            </a>{" "}
            ) 또는{" "}
            <a
              href="https://github.com/dani-garcia/vaultwarden"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              이 커뮤니티 버전
            </a>{" "}
            또는 유료 Bitwarden 계정이 필요합니다. 관련
            `SKYVERN_AUTH_BITWARDEN_*` 환경 변수가 구성되어 있는지 확인하세요.
            자세한 내용은{" "}
            <a
              href="https://docs.skyvern.com/credentials/bitwarden"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              여기
            </a>
            에서 확인하세요.
          </div>
        </div>
      )}
    </div>
  );
}

export { CredentialsPage };
