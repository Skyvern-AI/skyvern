import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const pageSizeArray = new Array(5).fill(null); // doesn't matter the value

function TaskListSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-1/3">URL</TableHead>
            <TableHead className="w-1/3">Status</TableHead>
            <TableHead className="w-1/3">Created At</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pageSizeArray.map((_, index) => {
            return (
              <TableRow key={index}>
                <TableCell className="w-1/3">
                  <Skeleton className="w-full h-4" />
                </TableCell>
                <TableCell className="w-1/3">
                  <Skeleton className="w-full h-4" />
                </TableCell>
                <TableCell className="w-1/3">
                  <Skeleton className="w-full h-4" />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

export { TaskListSkeleton };
