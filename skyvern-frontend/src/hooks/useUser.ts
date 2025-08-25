import { UserContext } from "@/store/UserContext";
import { useContext } from "react";

function useUser() {
  const getUser = useContext(UserContext);
  return { get: getUser };
}

export { useUser };
