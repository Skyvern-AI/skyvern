import { isPasswordCredential } from "@/api/types";
import { DeleteCredentialButton } from "./DeleteCredentialButton";
import { CredentialApiResponse } from "@/api/types";

type Props = {
  credential: CredentialApiResponse;
};

function CredentialItem({ credential }: Props) {
  const getTotpTypeDisplay = (totpType: string) => {
    switch (totpType) {
      case "authenticator":
        return "인증 앱";
      case "email":
        return "이메일";
      case "text":
        return "문자 메시지";
      case "none":
      default:
        return "";
    }
  };

  return (
    <div className="flex gap-5 rounded-lg bg-slate-elevation2 p-4">
      <div className="w-48 space-y-2">
        <p className="w-full truncate" title={credential.name}>
          {credential.name}
        </p>
        <p className="text-sm text-slate-400">{credential.credential_id}</p>
      </div>
      {isPasswordCredential(credential.credential) ? (
        <div className="border-l pl-5">
          <div className="flex gap-5">
            <div className="shrink-0 space-y-2">
              <p className="text-sm text-slate-400">사용자명/이메일</p>
              <p className="text-sm text-slate-400">비밀번호</p>
              {credential.credential.totp_type !== "none" && (
                <p className="text-sm text-slate-400">2FA 유형</p>
              )}
            </div>
            <div className="space-y-2">
              <p className="text-sm">{credential.credential.username}</p>
              <p className="text-sm">{"********"}</p>
              {credential.credential.totp_type !== "none" && (
                <p className="text-sm text-blue-400">
                  {getTotpTypeDisplay(credential.credential.totp_type)}
                </p>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex gap-5 border-l pl-5">
          <div className="flex gap-5">
            <div className="shrink-0 space-y-2">
              <p className="text-sm text-slate-400">카드 번호</p>
              <p className="text-sm text-slate-400">브랜드</p>
            </div>
          </div>
          <div className="flex gap-5">
            <div className="shrink-0 space-y-2">
              <p className="text-sm">
                {"************" + credential.credential.last_four}
              </p>
              <p className="text-sm">{credential.credential.brand}</p>
            </div>
          </div>
        </div>
      )}
      <div className="ml-auto">
        <DeleteCredentialButton credential={credential} />
      </div>
    </div>
  );
}

export { CredentialItem };
