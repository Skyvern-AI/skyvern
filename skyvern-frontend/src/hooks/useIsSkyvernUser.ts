import { useUser } from "./useUser";

function useIsSkyvernUser() {
  const user = useUser().get();
  const email = user?.email;
  const isSkyvernUser = email?.toLowerCase().endsWith("@skyvern.com") ?? false;

  return isSkyvernUser;
}

export { useIsSkyvernUser };
