import { Component, type ReactNode } from "react";

type Props = {
  onError: () => void;
  children: ReactNode;
};

type State = { hasError: boolean };

class OnboardingErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(): void {
    this.props.onError();
  }

  render(): ReactNode {
    return this.state.hasError ? null : this.props.children;
  }
}

export { OnboardingErrorBoundary };
