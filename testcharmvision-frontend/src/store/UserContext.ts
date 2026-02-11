import { User } from "@/api/types";
import { createContext } from "react";

type GetUser = () => User | null;
const UserContext = createContext<GetUser>(() => null);

export { UserContext };
