import type { ComponentType } from "react";
import type { NodeProps } from "@xyflow/react";

import { Collapsible } from "@/components/ui/collapsible";

import { CollapseContext } from "./CollapseContext";
import { useIsBlockCollapsed } from "./useNodeCollapseStore";

// Wraps a node component in a Radix Collapsible.Root and exposes the open
// state via CollapseContext. Each node renders its own NodeHeader (always
// visible) plus a NodeBody (CollapsibleContent) that animates height.
export function withCollapsible<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function Collapsed(props: P) {
    const label =
      typeof (props.data as { label?: unknown })?.label === "string"
        ? (props.data as { label: string }).label
        : "";
    // Empty label can only happen during a transient render before the
    // backend-required label is set; falling back to the global key would
    // make every unlabeled block share collapse state, so treat it as
    // open until the label arrives.
    const collapsedFromStore = useIsBlockCollapsed(label);
    const isCollapsed = label.length > 0 && collapsedFromStore;
    const open = !isCollapsed;
    return (
      <CollapseContext.Provider value={{ open }}>
        <Collapsible open={open}>
          <Component {...props} />
        </Collapsible>
      </CollapseContext.Provider>
    );
  }
  Collapsed.displayName = `withCollapsible(${
    Component.displayName ?? Component.name ?? "Component"
  })`;
  return Collapsed;
}
