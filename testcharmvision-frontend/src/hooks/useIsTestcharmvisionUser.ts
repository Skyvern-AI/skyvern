import { useUser } from "./useUser";

function useIsTestcharmvisionUser() {
  const user = useUser().get();
  const email = user?.email;
  const isTestcharmvisionUser = email?.toLowerCase().endsWith("@testcharmvision.com") ?? false;

  return isTestcharmvisionUser;
}

export { useIsTestcharmvisionUser };
