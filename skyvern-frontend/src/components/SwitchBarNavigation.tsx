import { cn } from "@/util/utils";
import { NavLink, useSearchParams } from "react-router-dom";

type Option = {
  label: string;
  to: string;
};

type Props = {
  options: Option[];
};

function SwitchBarNavigation({ options }: Props) {
  const [searchParams] = useSearchParams();

  return (
    <div className="flex w-fit gap-2 rounded-sm border border-slate-700 p-2">
      {options.map((option) => {
        return (
          <NavLink
            to={`${option.to}?${searchParams.toString()}`}
            replace
            key={option.to}
            className={({ isActive }) => {
              return cn(
                "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
                {
                  "bg-slate-700": isActive,
                },
              );
            }}
          >
            {option.label}
          </NavLink>
        );
      })}
    </div>
  );
}

export { SwitchBarNavigation };
