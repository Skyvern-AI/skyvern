import { useContext } from "react";

import CloudContext from "@/store/CloudContext";
import { useCredentialsQuery } from "./useCredentialsQuery";

function useSkyvernCredentialSourceAvailable(): boolean {
  const isCloud = useContext(CloudContext);
  const credentialsQuery = useCredentialsQuery({
    enabled: !isCloud,
    page_size: 100,
  });

  return isCloud || credentialsQuery.isSuccess;
}

export { useSkyvernCredentialSourceAvailable };
