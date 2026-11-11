import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { workflowBlockTitle } from "../nodes/types";
import { docBlockType, docStepCaption, docStepTitle } from "./docStepRegistry";

export function docProblemStep(node: AppNode) {
  const blockType = isWorkflowBlockNode(node) ? docBlockType(node) : null;
  return {
    type: blockType ? workflowBlockTitle[blockType] : null,
    name: docStepCaption(node) ?? docStepTitle(node),
  };
}
