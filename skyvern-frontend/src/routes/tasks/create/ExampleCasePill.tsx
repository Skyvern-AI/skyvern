type Props = {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
};

function ExampleCasePill({ icon, label, onClick }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex items-center gap-2 whitespace-normal rounded-full border border-border bg-card px-3 py-2 text-sm shadow-sm transition-shadow hover:shadow-card-hover lg:whitespace-nowrap"
    >
      <span className="flex size-7 items-center justify-center rounded-full bg-brand-soft text-brand transition-colors group-hover:bg-brand-cta group-hover:text-brand-cta-foreground [&>svg]:size-3.5">
        {icon}
      </span>
      <span>{label}</span>
    </button>
  );
}

export { ExampleCasePill };
