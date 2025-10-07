import { CheckIcon, Cross2Icon } from "@radix-ui/react-icons";

interface Props {
  children: React.ReactNode;
  offset?: string;
  failure?: boolean;
  success?: boolean;
}

function ItemStatusIndicator({
  children,
  offset = "-0.6rem",
  failure,
  success,
}: Props) {
  return (
    <div className="relative flex items-center justify-center overflow-visible">
      {children}
      {success && (
        <CheckIcon
          className="absolute h-3 w-3 text-success"
          style={{ right: offset, top: offset }}
        />
      )}
      {failure && (
        <Cross2Icon
          className="absolute h-[0.65rem] w-[0.65rem] text-destructive"
          style={{ right: offset, top: offset }}
        />
      )}
    </div>
  );
}

export { ItemStatusIndicator };
