import { Button } from "@/components/ui/button";
import { ExitIcon, PersonIcon } from "@radix-ui/react-icons";

type Props = {
  name: string;
};

function Profile({ name }: Props) {
  return (
    <div className="flex items-center border-2 p-2 rounded-lg">
      <div className="flex gap-2 items-center">
        <PersonIcon className="h-4 w-4" />
        <p className="w-40 overflow-hidden text-ellipsis">{name}</p>
      </div>
      <div>
        <Button variant="outline" size="icon">
          <ExitIcon className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

export { Profile };
