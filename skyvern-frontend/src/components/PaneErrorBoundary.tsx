import { Component, type ErrorInfo, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";

type Props = {
  pane: string;
  onError: (error: unknown, componentStack?: string | null) => void;
  fallbackContainer?: HTMLElement | null;
  children: ReactNode;
};

type State = {
  crashed: boolean;
};

class PaneErrorBoundary extends Component<Props, State> {
  state: State = { crashed: false };

  static getDerivedStateFromError(): State {
    return { crashed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    this.props.onError(error, info.componentStack);
  }

  private reload = () => {
    this.setState({ crashed: false });
  };

  render() {
    if (!this.state.crashed) {
      return this.props.children;
    }

    const fallback = (
      <div
        aria-label={`${this.props.pane} panel error`}
        className="flex h-full min-h-32 w-full items-center justify-center p-4"
      >
        <div className="flex flex-col items-center gap-3 text-center">
          <p className="text-sm text-muted-foreground">
            This panel hit an error.
          </p>
          <Button size="sm" variant="outline" onClick={this.reload}>
            Reload panel
          </Button>
        </div>
      </div>
    );

    return this.props.fallbackContainer
      ? createPortal(fallback, this.props.fallbackContainer)
      : fallback;
  }
}

export { PaneErrorBoundary };
