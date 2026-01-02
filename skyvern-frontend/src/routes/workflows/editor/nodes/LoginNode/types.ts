import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type LoginNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  disableCache: boolean;
  completeCriterion: string;
  terminateCriterion: string;
  engine: RunEngine | null;
};

export type LoginNode = Node<LoginNodeData, "login">;

export const loginNodeDefaultData: LoginNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("login"),
  label: "",
  url: "",
  navigationGoal:
    "If you're not on the login page, navigate to login page and login using the credentials given. First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page. If a 2-factor step appears, enter the authentication code. If you fail to login to find the login page or can't login after several trials, terminate. If login is completed, you're successful. ",
  errorCodeMapping: "null",
  maxRetries: null,
  maxStepsOverride: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  disableCache: false,
  completeCriterion: "",
  terminateCriterion: "",
  engine: RunEngine.SkyvernV1,
  model: null,
} as const;

export function isLoginNode(node: Node): node is LoginNode {
  return node.type === "login";
}
