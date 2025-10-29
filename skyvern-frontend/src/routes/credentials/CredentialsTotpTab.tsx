import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { PushTotpCodeForm } from "@/components/PushTotpCodeForm";
import { useTotpCodesQuery } from "@/hooks/useTotpCodesQuery";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import type { OtpType, TotpCode } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";

type OtpTypeFilter = "all" | OtpType;

const LIMIT_OPTIONS = [25, 50, 100] as const;

function formatDateTime(value: string | null): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function renderCodeContent(code: TotpCode): string {
  if (!code.code) {
    return "—";
  }
  return code.code;
}

function CredentialsTotpTab() {
  const [identifierFilter, setIdentifierFilter] = useState("");
  const [otpTypeFilter, setOtpTypeFilter] = useState<OtpTypeFilter>("all");
  const [limit, setLimit] = useState<(typeof LIMIT_OPTIONS)[number]>(50);

  const queryClient = useQueryClient();

  const queryParams = useMemo(() => {
    return {
      totp_identifier: identifierFilter.trim() || undefined,
      otp_type: otpTypeFilter === "all" ? undefined : otpTypeFilter,
      limit,
    };
  }, [identifierFilter, limit, otpTypeFilter]);

  const { data, isLoading, isFetching, isFeatureUnavailable } =
    useTotpCodesQuery({
      params: queryParams,
    });

  const codes = data ?? [];
  const hasFilters =
    identifierFilter.trim() !== "" || otpTypeFilter !== "all" || limit !== 50;

  const handleFormSuccess = () => {
    void queryClient.invalidateQueries({
      queryKey: ["totpCodes"],
    });
  };

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-slate-700 bg-slate-elevation1 p-6">
        <h2 className="text-lg font-semibold">Push a 2FA Code</h2>
        <p className="mt-1 text-sm text-slate-400">
          Paste the verification message you received. Skyvern extracts the code
          and attaches it to the relevant run.
        </p>
        <PushTotpCodeForm
          className="mt-4"
          showAdvancedFields
          onSuccess={handleFormSuccess}
        />
      </div>

      <div className="space-y-4">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div className="flex flex-wrap gap-4">
            <div className="space-y-1">
              <Label htmlFor="totp-identifier-filter">Identifier</Label>
              <Input
                id="totp-identifier-filter"
                placeholder="Filter by email or phone"
                value={identifierFilter}
                onChange={(event) => setIdentifierFilter(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="totp-type-filter">OTP Type</Label>
              <Select
                value={otpTypeFilter}
                onValueChange={(value: OtpTypeFilter) =>
                  setOtpTypeFilter(value)
                }
              >
                <SelectTrigger id="totp-type-filter" className="w-40">
                  <SelectValue placeholder="All types" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All types</SelectItem>
                  <SelectItem value="totp">Numeric code</SelectItem>
                  <SelectItem value="magic_link">Magic link</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="totp-limit-filter">Limit</Label>
              <Select
                value={String(limit)}
                onValueChange={(value) =>
                  setLimit(Number(value) as (typeof LIMIT_OPTIONS)[number])
                }
              >
                <SelectTrigger id="totp-limit-filter" className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {LIMIT_OPTIONS.map((option) => (
                    <SelectItem key={option} value={String(option)}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setIdentifierFilter("");
              setOtpTypeFilter("all");
              setLimit(50);
            }}
            disabled={!hasFilters}
          >
            Clear filters
          </Button>
        </div>

        {isFeatureUnavailable && (
          <Alert variant="destructive">
            <AlertTitle>2FA listing unavailable</AlertTitle>
            <AlertDescription>
              Upgrade the backend to include{" "}
              <code>GET /v1/credentials/totp</code>. Once available, this tab
              will automatically populate with codes.
            </AlertDescription>
          </Alert>
        )}

        {!isFeatureUnavailable && (
          <div className="rounded-lg border border-slate-700 bg-slate-elevation1">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[220px]">Identifier</TableHead>
                  <TableHead>Code</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Workflow Run</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Expires</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading || isFetching ? (
                  <TableRow>
                    <TableCell colSpan={6}>
                      <div className="space-y-2 p-2">
                        <Skeleton className="h-6 w-full" />
                        <Skeleton className="h-6 w-full" />
                        <Skeleton className="h-6 w-3/4" />
                      </div>
                    </TableCell>
                  </TableRow>
                ) : null}

                {!isLoading && !isFetching && codes.length === 0 ? (
                  <TableRow>
                    <TableCell
                      colSpan={6}
                      className="text-center text-sm text-slate-300"
                    >
                      No 2FA codes yet. Paste a verification message above or
                      configure automatic forwarding.
                    </TableCell>
                  </TableRow>
                ) : null}

                {!isLoading &&
                  !isFetching &&
                  codes.map((code) => (
                    <TableRow key={code.totp_code_id}>
                      <TableCell className="font-mono text-xs">
                        {code.totp_identifier ?? "—"}
                      </TableCell>
                      <TableCell className="font-semibold">
                        {renderCodeContent(code)}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {code.otp_type ?? "unknown"}
                        </Badge>
                        {code.source ? (
                          <span className="ml-2 text-xs text-slate-400">
                            {code.source}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-xs">
                        {code.workflow_run_id ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs">
                        {formatDateTime(code.created_at)}
                      </TableCell>
                      <TableCell className="text-xs">
                        {formatDateTime(code.expired_at)}
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  );
}

export { CredentialsTotpTab };
