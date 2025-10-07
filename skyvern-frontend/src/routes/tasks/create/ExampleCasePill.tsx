type Props = {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
};

function ExampleCasePill({ icon, label, onClick }: Props) {
  return (
    <div
      className="flex cursor-pointer gap-2 whitespace-normal rounded-sm bg-slate-elevation3 px-4 py-3 hover:bg-slate-elevation5 lg:whitespace-nowrap"
      onClick={onClick}
    >
      <div>{icon}</div>
      <div>{label}</div>
    </div>
  );
}

export { ExampleCasePill };
