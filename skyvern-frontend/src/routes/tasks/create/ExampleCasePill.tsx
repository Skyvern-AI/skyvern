import { cn } from "@/util/utils";

type Props = {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
};

function ExampleCasePill({ icon, label, onClick, disabled = false }: Props) {
  return (
    <div
      className={cn(
        "flex cursor-pointer gap-2 whitespace-normal rounded-sm bg-slate-elevation3 px-4 py-3 hover:bg-slate-elevation5 lg:whitespace-nowrap",
        disabled && "pointer-events-none opacity-50",
      )}
      aria-disabled={disabled}
      onClick={() => {
        if (disabled) {
          return;
        }
        onClick();
      }}
    >
      <div>{icon}</div>
      <div>{label}</div>
    </div>
  );
}

export { ExampleCasePill };
