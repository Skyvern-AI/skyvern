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
        return "Authenticator App";
      case "email":
        return "Email";
      case "text":
        return "Text Message";
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
              <p className="text-sm text-slate-400">Username/Email</p>
              <p className="text-sm text-slate-400">Password</p>
              {credential.credential.totp_type !== "none" && (
                <p className="text-sm text-slate-400">2FA Type</p>
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
              <p className="text-sm text-slate-400">Card Number</p>
              <p className="text-sm text-slate-400">Brand</p>
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
