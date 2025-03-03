import { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type URLNodeData = NodeBaseData & {
  url: string;
};

export type URLNode = Node<URLNodeData, "url">;

export const urlNodeDefaultData: URLNodeData = {
  label: "",
  continueOnFailure: false,
  url: "",
  editable: true,
};

export function isUrlNode(node: Node): node is URLNode {
  return node.type === "url";
}
