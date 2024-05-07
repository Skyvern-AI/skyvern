import { CredentialGetter } from "@/api/AxiosClient";
import { createContext } from "react";

const CredentialGetterContext = createContext<CredentialGetter | null>(null);

export { CredentialGetterContext };
