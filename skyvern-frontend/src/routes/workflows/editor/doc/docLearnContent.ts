export type DocLearnContent = {
  id: string;
  title: string;
  description: string;
};

export const docLearnContent: DocLearnContent[] = [
  {
    id: "document",
    title: "How a document becomes a workflow",
    description:
      "Each step in this document is a workflow block, shown as text with its type above it. Loops repeat the steps indented under them. A conditional checks its branches left to right and runs the first that matches, or Otherwise. Visual, YAML and this document show the same workflow.",
  },
  {
    id: "references",
    title: "Referencing data",
    description:
      "Grey pills are references to an input or an earlier step’s output, written {{ name }} in the workflow. Inside a loop, current_index identifies the iteration. For-each loops also provide current_value for the current item; while loops do not.",
  },
  {
    id: "notes",
    title: "Notes",
    description:
      "Plain text between steps is a note. The agent reads it at run time, in the step above it and the step below it. Text after the last step of a loop or branch is read by that last step.",
  },
];
