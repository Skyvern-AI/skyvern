import type { FailureCategory } from "@/api/types";
import { Badge } from "@/components/ui/badge";

function formatCategoryLabel(category: string): string {
  return category
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

type Props = {
  failureCategory: Array<FailureCategory> | null;
};

function FailureCategoryBadge({ failureCategory }: Props) {
  const primary = failureCategory?.[0];
  if (!primary) {
    return null;
  }
  return (
    <Badge variant="destructive" className="w-fit">
      {formatCategoryLabel(primary.category)}
    </Badge>
  );
}

export { FailureCategoryBadge };
