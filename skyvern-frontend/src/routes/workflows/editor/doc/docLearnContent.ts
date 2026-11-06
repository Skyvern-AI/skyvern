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
  {
    id: "keys",
    title: "Keyboard shortcuts",
    description:
      "Enter starts a new step of the same type below; on an empty step it turns the line back into a note. Shift+Enter adds a line break inside a step. Enter in the middle of a step splits it in two.\n\n/ at the start of a line changes the step type. @ inserts an input or an earlier result. Tab nests a line under the loop or If above it, Shift+Tab moves it out; under an empty loop it opens the step menu, because no type is picked for you.\n\nUp and Down move between lines. Cmd+Up and Cmd+Down (or Alt+Shift+Up and Down) move the step. Cmd+. opens the step’s settings: Tab moves through them and Esc returns to the step’s text.\n\nCmd+Enter runs the step. Cmd+Shift+Enter runs the whole agent: Tab between the inputs, Enter runs. Cmd+S saves. Cmd+Z and Cmd+Shift+Z undo and redo. On Windows, use Ctrl in place of Cmd.",
  },
];
