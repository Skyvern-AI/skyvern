import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/util/utils";
import { useState } from "react";

type Props = {
  title: string;
  description: string;
  body: string;
  onClick: () => void;
};

function TaskTemplateCard({ title, description, body, onClick }: Props) {
  const [hovering, setHovering] = useState(false);

  return (
    <Card
      className="border-0"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onMouseOver={() => setHovering(true)}
      onMouseOut={() => setHovering(false)}
    >
      <CardHeader
        className={cn("rounded-t-md bg-slate-elevation1", {
          "bg-slate-900": hovering,
        })}
      >
        <CardTitle className="font-normal">{title}</CardTitle>
        <CardDescription className="overflow-hidden text-ellipsis whitespace-nowrap text-slate-400">
          {description}
        </CardDescription>
      </CardHeader>
      <CardContent
        className={cn(
          "h-36 cursor-pointer rounded-b-md bg-slate-elevation3 p-4 text-sm text-slate-300",
          {
            "bg-slate-800": hovering,
          },
        )}
        onClick={() => {
          onClick();
        }}
      >
        {body}
      </CardContent>
    </Card>
  );
}

export { TaskTemplateCard };
