// own file so dashboard-load-state.tsx only exports a component (react-refresh/only-export-components)
function isDashboardLoadStateActive({
  isLoading,
  isError,
  isEmpty,
}: {
  isLoading: boolean;
  isError: boolean;
  isEmpty: boolean;
}): boolean {
  return isLoading || isError || isEmpty;
}

export { isDashboardLoadStateActive };
