type Props = {
  onClick: () => void;
};

function TemplateExpressionRow({ onClick }: Props) {
  return (
    <div className="border-t border-border">
      <button
        type="button"
        onClick={onClick}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-foreground hover:bg-muted dark:text-slate-200 dark:hover:bg-slate-700"
      >
        <span className="font-mono text-xs">{"{{}}"}</span>
        <span>Use template expression</span>
      </button>
    </div>
  );
}

export { TemplateExpressionRow };
