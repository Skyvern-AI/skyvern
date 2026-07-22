function EmptyState() {
  return (
    <div className="flex items-center justify-center px-6 py-12 text-center">
      <div className="space-y-1.5">
        <div className="text-sm text-tertiary-foreground">
          No block selected
        </div>
        <div className="text-xs text-muted-foreground dark:text-slate-500">
          Click a row in the timeline to see its details here.
        </div>
      </div>
    </div>
  );
}

export { EmptyState };
