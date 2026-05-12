import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItem,
  SelectItemText,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrowserProfilesQuery } from "../hooks/useBrowserProfilesQuery";

const NONE_VALUE = "__none__";

type Props = {
  value: string | null;
  onChange: (value: string | null) => void;
  placeholder?: string;
};

function BrowserProfileSelector({ value, onChange, placeholder }: Props) {
  const { data: profiles, isLoading, isError } = useBrowserProfilesQuery();

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }

  if (isError) {
    return (
      <Select disabled>
        <SelectTrigger>
          <SelectValue placeholder="Failed to load browser profiles" />
        </SelectTrigger>
      </Select>
    );
  }

  const selectValue = value === null || value === "" ? NONE_VALUE : value;

  return (
    <Select
      value={selectValue}
      onValueChange={(next) => {
        onChange(next === NONE_VALUE ? null : next);
      }}
    >
      <SelectTrigger>
        <SelectValue placeholder={placeholder ?? "Select a browser profile"} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={NONE_VALUE}>None</SelectItem>
        {profiles && profiles.length > 0 && <SelectSeparator />}
        {profiles?.map((profile) => (
          <CustomSelectItem
            key={profile.browser_profile_id}
            value={profile.browser_profile_id}
          >
            <div className="space-y-1">
              <p className="text-sm font-medium">
                <SelectItemText>{profile.name}</SelectItemText>
              </p>
              {profile.description && (
                <p className="text-xs text-slate-400">{profile.description}</p>
              )}
            </div>
          </CustomSelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { BrowserProfileSelector };
