import type { ComponentType } from "react";
import { EnvelopeClosedIcon } from "@radix-ui/react-icons";
import { GoogleSheetsIcon } from "@/components/icons/GoogleSheetsIcon";
import { GmailIcon } from "@/components/icons/GmailIcon";
import { OutlookIcon } from "@/components/icons/OutlookIcon";
import { RobotIcon } from "@/components/icons/RobotIcon";
import { GoogleDocsIcon } from "@/components/icons/GoogleDocsIcon";
import { GoogleDriveIcon } from "@/components/icons/GoogleDriveIcon";
import { NotionIcon } from "@/components/icons/NotionIcon";
import { N8nIcon } from "@/components/icons/N8nIcon";
import { ZapierIcon } from "@/components/icons/ZapierIcon";
import { MakeIcon } from "@/components/icons/MakeIcon";
import { WorkatoIcon } from "@/components/icons/WorkatoIcon";
import { KeyIcon } from "@/components/icons/KeyIcon";
import { MessageIcon } from "@/components/icons/MessageIcon";
import { OnePasswordIcon } from "@/components/icons/OnePasswordIcon";
import { BitwardenIcon } from "@/components/icons/BitwardenIcon";
import { AzureKeyVaultIcon } from "@/components/icons/AzureKeyVaultIcon";
import { PowerBIIcon } from "@/components/icons/PowerBIIcon";

type IntegrationLogo = ComponentType<{ className?: string }>;

const INTEGRATION_LOGOS: Readonly<Record<string, IntegrationLogo>> = {
  google_sheets: GoogleSheetsIcon,
  gmail: GmailIcon,
  outlook: OutlookIcon,
  robot: RobotIcon,
  google_docs: GoogleDocsIcon,
  google_drive: GoogleDriveIcon,
  notion: NotionIcon,
  n8n: N8nIcon,
  zapier: ZapierIcon,
  make: MakeIcon,
  workato: WorkatoIcon,
  key: KeyIcon,
  message: MessageIcon,
  envelope: EnvelopeClosedIcon,
  onepassword: OnePasswordIcon,
  bitwarden: BitwardenIcon,
  azure_key_vault: AzureKeyVaultIcon,
  powerbi: PowerBIIcon,
};

export function integrationLogo(
  key: string | undefined,
): IntegrationLogo | undefined {
  return key ? INTEGRATION_LOGOS[key] : undefined;
}
