import { useContext } from "react";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableMessageRow,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { OidcAuthContext } from "@/store/OnPremOidcContext";
import { usePageSlots } from "@/store/PageSlots";
import { normalizeUtcTimestamp } from "@/util/timeFormat";

type OrganizationRbacMember = Readonly<{
  user_id: string;
  clerk_role: string | null;
  app_role: string | null;
  effective_role: string | null;
  can_manage_billing: boolean | null;
  last_seen_at?: string | null;
}>;

type OrganizationRbacResponse = Readonly<{
  enabled: boolean;
  eligible: boolean;
  enforce: boolean;
  suspended_at: string | null;
  roles: ReadonlyArray<string>;
  grants?: Readonly<Record<string, ReadonlyArray<string>>> | null;
  members?: ReadonlyArray<OrganizationRbacMember> | null;
}>;

const MANAGE_ORGANIZATION_CAPABILITY = "organization.rbac.manage";

function displayRole(role: string | null): string {
  if (role === null) return "Not available";
  return role
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatLastSeenAt(value: string | null | undefined): string {
  if (!value) return "No sign-in recorded";
  const date = new Date(normalizeUtcTimestamp(value));
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function OrganizationMembers() {
  const { enabled: oidcEnabled } = useContext(OidcAuthContext);
  const { permissions } = usePageSlots();
  const credentialGetter = useCredentialGetter();
  const canManageOrganization =
    permissions?.status === "ready" &&
    permissions.data.capabilities.some(
      (permission) =>
        permission.capability === MANAGE_ORGANIZATION_CAPABILITY &&
        permission.allowed,
    );
  const query = useQuery({
    queryKey: ["organization-rbac-members"],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const { data } = await client.get<OrganizationRbacResponse>(
        "/organizations/rbac",
      );
      return data;
    },
    enabled: oidcEnabled && canManageOrganization,
    retry: false,
  });

  if (!oidcEnabled || !canManageOrganization) return null;

  const members = query.data?.members ?? [];

  return (
    <Card>
      <CardHeader className="border-b-2">
        <CardTitle className="text-lg">Members</CardTitle>
        <CardDescription>
          Read-only list of signed-in members, their effective role, and last
          sign-in.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-8">
        <p className="text-sm text-muted-foreground">
          Roles are assigned by your identity provider.
        </p>
        {query.isPending ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading members…
          </p>
        ) : query.isError ? (
          <p role="alert" className="text-sm text-destructive">
            Members could not be loaded.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>User ID</TableHead>
                <TableHead>Effective role</TableHead>
                <TableHead>Last sign-in</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.length === 0 ? (
                <TableMessageRow colSpan={3}>No members found.</TableMessageRow>
              ) : (
                members.map((member) => (
                  <TableRow key={member.user_id}>
                    <TableCell className="font-mono text-xs">
                      {member.user_id}
                    </TableCell>
                    <TableCell>{displayRole(member.effective_role)}</TableCell>
                    <TableCell>
                      {formatLastSeenAt(member.last_seen_at)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export { OrganizationMembers };
