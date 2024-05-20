import { Skeleton } from "@/components/ui/skeleton";
import { TableCell, TableRow } from "@/components/ui/table";

const pageSizeArray = new Array(5).fill(null); // doesn't matter the value

function TaskListSkeletonRows() {
  return pageSizeArray.map((_, index) => {
    return (
      <TableRow key={index}>
        <TableCell className="w-1/3">
          <Skeleton className="w-full h-6" />
        </TableCell>
        <TableCell className="w-1/4">
          <Skeleton className="w-full h-6" />
        </TableCell>
        <TableCell className="w-1/3">
          <Skeleton className="w-full h-6" />
        </TableCell>
        <TableCell className="w-1/12">
          <Skeleton className="w-full h-6" />
        </TableCell>
      </TableRow>
    );
  });
}

export { TaskListSkeletonRows };
