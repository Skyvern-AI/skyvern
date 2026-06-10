import { useState } from "react";
import { Pencil1Icon } from "@radix-ui/react-icons";
import { useNavigate } from "react-router-dom";

import { BrowserProfileApiResponse } from "@/api/types";
import { SelectionCheckboxCell } from "@/components/SelectionCheckbox";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { basicTimeFormat, compactLocalDateTime } from "@/util/timeFormat";

import { DeleteBrowserProfileButton } from "./DeleteBrowserProfileButton";
import { RenameBrowserProfileDialog } from "./RenameBrowserProfileDialog";

type Props = {
  profile: BrowserProfileApiResponse;
  index?: number;
  selected?: boolean;
  hasSelection?: boolean;
  onSelect?: (index: number, shiftKey: boolean) => void;
};

function BrowserProfileItem({
  profile,
  index = -1,
  selected = false,
  hasSelection = false,
  onSelect,
}: Props) {
  const navigate = useNavigate();
  const [renameOpen, setRenameOpen] = useState(false);

  const handleRowClick = (event: React.MouseEvent<HTMLTableRowElement>) => {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        `${window.location.origin}/browser-profiles/${profile.browser_profile_id}`,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    navigate(`/browser-profiles/${profile.browser_profile_id}`);
  };

  const stopRowClick = (event: React.MouseEvent<HTMLElement>) => {
    event.stopPropagation();
  };

  return (
    <TableRow
      className="group/row cursor-pointer"
      data-state={selected ? "selected" : undefined}
      onClick={handleRowClick}
    >
      {onSelect && (
        <SelectionCheckboxCell
          index={index}
          checked={selected}
          hasSelection={hasSelection}
          onSelect={onSelect}
          ariaLabel={`Select ${profile.name}`}
        />
      )}
      <TableCell className="truncate">
        <span title={profile.name}>{profile.name}</span>
      </TableCell>
      <TableCell className="truncate text-muted-foreground">
        {profile.description ? (
          <span title={profile.description}>{profile.description}</span>
        ) : (
          <span className="opacity-50">—</span>
        )}
      </TableCell>
      <TableCell className="truncate text-muted-foreground">
        {profile.source_browser_type ?? <span className="opacity-50">—</span>}
      </TableCell>
      <TableCell
        className="text-muted-foreground"
        title={basicTimeFormat(profile.created_at)}
      >
        {compactLocalDateTime(profile.created_at)}
      </TableCell>
      <TableCell onClick={stopRowClick}>
        <div className="flex justify-end gap-2">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => setRenameOpen(true)}
                  aria-label="Rename browser profile"
                  className="text-muted-foreground hover:text-foreground"
                >
                  <Pencil1Icon className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Rename Browser Profile</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <DeleteBrowserProfileButton profile={profile} />
        </div>
        <RenameBrowserProfileDialog
          profile={profile}
          open={renameOpen}
          onOpenChange={setRenameOpen}
        />
      </TableCell>
    </TableRow>
  );
}

export { BrowserProfileItem };
