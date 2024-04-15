import { User } from "@/api/types";
import { createContext } from "react";

const UserContext = createContext<User | null>(null);

export { UserContext };
