import {
  BookmarkIcon,
  CalendarIcon,
  CounterClockwiseClockIcon,
  DesktopIcon,
  DotsHorizontalIcon,
  GearIcon,
  GlobeIcon,
  IdCardIcon,
  LightningBoltIcon,
  ListBulletIcon,
  LockClosedIcon,
  PersonIcon,
  PlusIcon,
  ReaderIcon,
  ReloadIcon,
  Share1Icon,
} from "@radix-ui/react-icons";

import { BagIcon } from "@/components/icons/BagIcon";
import { CompassIcon } from "@/components/icons/CompassIcon";
import { DocumentIcon } from "@/components/icons/DocumentIcon";
import { GovernmentIcon } from "@/components/icons/GovernmentIcon";
import { HospitalIcon } from "@/components/icons/HospitalIcon";
import { InboxIcon } from "@/components/icons/InboxIcon";
import { KeyIcon } from "@/components/icons/KeyIcon";
import { LogisticsIcon } from "@/components/icons/LogisticsIcon";
import { N8nIcon } from "@/components/icons/N8nIcon";
import { ReceiptIcon } from "@/components/icons/ReceiptIcon";
import { RobotIcon } from "@/components/icons/RobotIcon";
import {
  SidebarTreeNav,
  type SidebarNavItem,
} from "@/components/SidebarTreeNav";
import { defaultWorkflowRequest } from "@/routes/workflows/defaultWorkflowRequest";
import { useCreateWorkflowMutation } from "@/routes/workflows/hooks/useCreateWorkflowMutation";
import { shouldDefaultRecipesOpen } from "./sidebarDefaults";
import { usePostHog } from "posthog-js/react";

const recipeAnalyticsByPath: Record<string, { beta: boolean; badge?: string }> =
  {
    "/recipes/healthcare": { beta: true, badge: "Beta" },
    "/recipes/government": { beta: true, badge: "Beta" },
    "/recipes/invoices": { beta: true, badge: "Beta" },
    "/recipes/insurance": { beta: true, badge: "Beta" },
    "/recipes/purchasing": { beta: true, badge: "Beta" },
    "/recipes/crm": { beta: true, badge: "Beta" },
    "/recipes/logistics": { beta: true, badge: "Beta" },
    "/recipes/contact-forms": { beta: true, badge: "Beta" },
    "/recipes/job-apps": { beta: true, badge: "Beta" },
  };

type Props = {
  collapsed?: boolean;
};

function SideNav({ collapsed }: Props = {}) {
  const createWorkflowMutation = useCreateWorkflowMutation();
  const postHog = usePostHog();
  const captureRecipeClick = (label: string, to: string) => {
    const analytics = recipeAnalyticsByPath[to] ?? { beta: false };
    postHog?.capture("sidebar.agent.clicked", {
      agent: label
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, ""),
      destination: to,
      disabled: false,
      ...analytics,
    });
  };
  const navItems: Array<SidebarNavItem> = [
    {
      label: "Home",
      to: "/discover",
      icon: <CompassIcon className="size-4" />,
    },
    {
      label: "Agents",
      to: "/workflows",
      icon: <LightningBoltIcon className="size-4" />,
      children: [
        {
          label: createWorkflowMutation.isPending ? "Creating..." : "New Agent",
          icon: createWorkflowMutation.isPending ? (
            <ReloadIcon className="size-3.5 animate-spin" />
          ) : (
            <PlusIcon className="size-3.5" />
          ),
          onClick: () => {
            if (createWorkflowMutation.isPending) {
              return;
            }
            createWorkflowMutation.mutate({
              ...defaultWorkflowRequest,
              _via: "sidebar",
            });
          },
          disabled: createWorkflowMutation.isPending,
        },
        {
          label: "All Agents",
          to: "/workflows",
          icon: <ListBulletIcon className="size-3.5" />,
        },
        {
          label: "Schedules",
          to: "/schedules",
          icon: <CalendarIcon className="size-3.5" />,
        },
        {
          label: "Runs",
          to: "/runs",
          icon: <CounterClockwiseClockIcon className="size-3.5" />,
        },
      ],
    },
    {
      label: "Recipes",
      to: "/recipes",
      badge: "Beta",
      icon: <BookmarkIcon className="size-4" />,
      defaultOpen: shouldDefaultRecipesOpen,
      initialVisibleChildren: 3,
      children: [
        {
          label: "Healthcare",
          to: "/recipes/healthcare",
          onClick: () =>
            captureRecipeClick("Healthcare", "/recipes/healthcare"),
          icon: <HospitalIcon className="size-3.5" />,
        },
        {
          label: "Government",
          to: "/recipes/government",
          onClick: () =>
            captureRecipeClick("Government", "/recipes/government"),
          icon: <GovernmentIcon className="size-3.5" />,
        },
        {
          label: "Invoices",
          to: "/recipes/invoices",
          onClick: () => captureRecipeClick("Invoices", "/recipes/invoices"),
          icon: <ReceiptIcon className="size-3.5" />,
        },
        {
          label: "Insurance",
          to: "/recipes/insurance",
          onClick: () => captureRecipeClick("Insurance", "/recipes/insurance"),
          icon: <DocumentIcon className="size-3.5" />,
        },
        {
          label: "Purchasing",
          to: "/recipes/purchasing",
          onClick: () =>
            captureRecipeClick("Purchasing", "/recipes/purchasing"),
          icon: <BagIcon className="size-3.5" />,
        },
        {
          label: "CRM",
          to: "/recipes/crm",
          onClick: () => captureRecipeClick("CRM", "/recipes/crm"),
          icon: <Share1Icon className="size-3.5" />,
        },
        {
          label: "Logistics",
          to: "/recipes/logistics",
          onClick: () => captureRecipeClick("Logistics", "/recipes/logistics"),
          icon: <LogisticsIcon className="size-3.5" />,
        },
        {
          label: "Contact Forms",
          to: "/recipes/contact-forms",
          onClick: () =>
            captureRecipeClick("Contact Forms", "/recipes/contact-forms"),
          icon: <ReaderIcon className="size-3.5" />,
        },
        {
          label: "Job Apps",
          to: "/recipes/job-apps",
          onClick: () => captureRecipeClick("Job Apps", "/recipes/job-apps"),
          icon: <InboxIcon className="size-3.5" />,
        },
      ],
    },
    {
      label: "Browsers",
      to: "/browser-sessions",
      icon: <GlobeIcon className="size-4" />,
      defaultOpen: false,
      children: [
        {
          label: "Sessions",
          to: "/browser-sessions",
          icon: <DesktopIcon className="size-3.5" />,
        },
        {
          label: "Profiles",
          to: "/browser-profiles",
          icon: <PersonIcon className="size-3.5" />,
        },
      ],
    },
    {
      label: "Credentials",
      to: "/credentials",
      icon: <KeyIcon className="size-4" />,
      defaultOpen: false,
      children: [
        {
          label: "Passwords",
          to: "/credentials?tab=passwords",
          icon: <LockClosedIcon className="size-3.5" />,
        },
        {
          label: "Credit Cards",
          to: "/credentials?tab=creditCards",
          icon: <IdCardIcon className="size-3.5" />,
        },
        {
          label: "Secrets",
          to: "/credentials?tab=secrets",
          icon: <KeyIcon className="size-3.5" />,
        },
        {
          label: "2FA",
          to: "/credentials?tab=twoFactor",
          icon: <CounterClockwiseClockIcon className="size-3.5" />,
        },
      ],
    },
    {
      label: "Integrations",
      to: "/integrations",
      icon: <Share1Icon className="size-4" />,
      defaultOpen: false,
      children: [
        {
          label: "MCP",
          to: "https://www.skyvern.com/docs/developers/getting-started/mcp",
          external: true,
          icon: <RobotIcon className="size-3.5" />,
        },
        {
          label: "1Password",
          to: "/integrations?query=1Password",
          icon: <LockClosedIcon className="size-3.5" />,
        },
        {
          label: "n8n",
          to: "/integrations?query=n8n",
          icon: <N8nIcon className="size-3.5" />,
        },
        {
          label: "More",
          to: "/integrations",
          icon: <DotsHorizontalIcon className="size-3.5" />,
        },
      ],
    },
    {
      label: "Settings",
      to: "/settings",
      icon: <GearIcon className="size-4" />,
      children: [
        {
          label: "API Keys",
          to: "/settings?section=api-keys",
          icon: <KeyIcon className="size-3.5" />,
        },
      ],
    },
  ];

  return <SidebarTreeNav items={navItems} collapsed={collapsed} />;
}

export { SideNav };
