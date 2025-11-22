import { useUser } from "./useUser";

function useIsSkyzonaUser() {
  const user = useUser().get();
  const email = user?.email;
  const isSkyzonaUser = email?.toLowerCase().endsWith("@skyzona.com") ?? false;

  return isSkyzonaUser;
}

export { useIsSkyzonaUser };
