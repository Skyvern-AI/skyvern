import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRightIcon } from "@radix-ui/react-icons";
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
import {
  OtpType,
  type OtpType as OtpTypeValue,
  type TotpCode,
} from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { GmailIcon } from "@/components/icons/GmailIcon";

type OtpTypeFilter = "all" | OtpTypeValue;

const LIMIT_OPTIONS = [25, 50, 100] as const;

function safeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

function otpTypeLabel(otpType: OtpTypeValue | null): string {
  switch (otpType) {
    case OtpType.Totp:
      return "Numeric code";
    case OtpType.MagicLink:
      return "Magic link";
    default:
      return "unknown";
  }
}

function renderCodeContent(code: TotpCode) {
  if (!code.code) {
    return "—";
  }
  if (code.otp_type === OtpType.MagicLink) {
    const href = safeHttpUrl(code.code);
    if (!href) {
      return (
        <span className="block max-w-xs truncate" title={code.code}>
          {code.code}
        </span>
      );
    }
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 underline underline-offset-2 hover:text-blue-300"
      >
        Open magic link
      </a>
    );
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
      <div className="rounded-lg border border-slate-700 bg-slate-elevation1 p-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-neutral-200 bg-white dark:border-slate-700">
              <GmailIcon className="size-7" />
            </div>
            <div>
              <h2 className="text-base font-semibold">
                Connect Gmail for automatic 2FA
              </h2>
              <p className="mt-1 max-w-2xl text-sm text-neutral-600 dark:text-slate-400">
                Skyvern can find verification codes and magic links in a
                connected Gmail inbox without manual forwarding.
              </p>
            </div>
          </div>
          <Button asChild size="sm" className="shrink-0">
            <Link to="/integrations?query=gmail">
              Connect Gmail <ArrowRightIcon className="ml-1.5 size-4" />
            </Link>
          </Button>
        </div>
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-elevation1 p-6">
        <h2 className="text-lg font-semibold">Push a 2FA Code</h2>
        <p className="mt-1 text-sm text-neutral-600 dark:text-slate-400">
          Paste the verification message you received. Skyvern extracts the code
          and attaches it to the relevant run.
        </p>
        <p className="mt-2 text-sm text-neutral-600 dark:text-slate-400">
          Prefer to send codes programmatically? See the{" "}
          <a
            href="https://docs.skyvern.com/api-reference/credentials/send-totp-code"
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 underline hover:text-blue-300"
          >
            API documentation
          </a>
          .
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
                  <SelectItem value={OtpType.Totp}>Numeric code</SelectItem>
                  <SelectItem value={OtpType.MagicLink}>Magic link</SelectItem>
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
                  <TableHead>Verification</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Agent Run</TableHead>
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
                      className="text-center text-sm text-neutral-600 dark:text-slate-300"
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
                          {otpTypeLabel(code.otp_type)}
                        </Badge>
                        {code.source ? (
                          <span className="ml-2 text-xs text-neutral-600 dark:text-slate-400">
                            {code.source}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-xs">
                        {code.workflow_run_id ?? "—"}
                      </TableCell>
                      <TableCell
                        className="text-xs"
                        title={basicTimeFormat(code.created_at)}
                      >
                        {basicLocalTimeFormat(code.created_at)}
                      </TableCell>
                      <TableCell
                        className="text-xs"
                        title={
                          code.expired_at
                            ? basicTimeFormat(code.expired_at)
                            : undefined
                        }
                      >
                        {code.expired_at
                          ? basicLocalTimeFormat(code.expired_at)
                          : "—"}
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
