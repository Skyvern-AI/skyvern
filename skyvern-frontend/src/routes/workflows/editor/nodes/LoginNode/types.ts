import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type LoginNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  cacheActions: boolean;
};

export type LoginNode = Node<LoginNodeData, "login">;

export const loginNodeDefaultData: LoginNodeData = {
  label: "",
  url: "",
  navigationGoal:
    "If you're not on the login page, navigate to login page and login using the credentials given. First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page. If you fail to login to find the login page or can't login after several trials, terminate. If login is completed, you're successful. ",
  errorCodeMapping: "null",
  maxRetries: null,
  maxStepsOverride: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  cacheActions: false,
} as const;

export function isLoginNode(node: Node): node is LoginNode {
  return node.type === "login";
}
