import { CredentialGetterContext } from "@/store/CredentialGetterContext";
import { useContext } from "react";

function useCredentialGetter() {
  const credentialGetter = useContext(CredentialGetterContext);
  return credentialGetter;
}

export { useCredentialGetter };
