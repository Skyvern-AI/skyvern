# Skyvern Frontend Architecture

This document provides a comprehensive overview of the Skyvern frontend codebase structure, patterns, and conventions.

## Directory Structure

```
skyvern-frontend/
├── src/
│   ├── api/                      # HTTP client and API utilities
│   │   ├── AxiosClient.ts        # Axios instances (v1, v2, sans-api-v1)
│   │   ├── QueryClient.ts        # React Query configuration
│   │   ├── types.ts              # API response/request types
│   │   ├── utils.ts              # API utility functions
│   │   └── sse.ts                # Server-Sent Events client
│   │
│   ├── components/               # Reusable UI components
│   │   ├── ui/                   # shadcn-style UI primitives
│   │   │   ├── button.tsx        # Button with CVA variants
│   │   │   ├── card.tsx          # Card container
│   │   │   ├── input.tsx         # Input field
│   │   │   ├── select.tsx        # Radix Select wrapper
│   │   │   ├── dialog.tsx        # Radix Dialog wrapper
│   │   │   ├── tabs.tsx          # Radix Tabs wrapper
│   │   │   ├── toast.tsx         # Toast notification
│   │   │   └── ...               # 30+ UI components
│   │   ├── icons/                # Icon components
│   │   ├── ThemeProvider.tsx     # Theme switching logic
│   │   ├── PageLayout.tsx        # Main page layout wrapper
│   │   └── [feature components]  # Domain-specific components
│   │
│   ├── hooks/                    # Custom React hooks
│   │   ├── useCredentialGetter.ts
│   │   ├── useCostCalculator.ts
│   │   └── [20+ other hooks]
│   │
│   ├── routes/                   # Page components by feature
│   │   ├── root/                 # Root layout
│   │   ├── browserSessions/      # Browser session management
│   │   ├── workflows/            # Workflow editor and runs
│   │   ├── tasks/                # Task management
│   │   ├── credentials/          # Credentials management
│   │   ├── runs/                 # Run history
│   │   ├── discover/             # Discovery page
│   │   └── history/              # History page
│   │
│   ├── store/                    # Global state management
│   │   ├── use*Store.ts          # Zustand stores
│   │   └── *Context.ts           # React Contexts
│   │
│   ├── util/                     # Utility functions
│   │   ├── utils.ts              # cn() helper, formatters
│   │   └── env.ts                # Environment helpers
│   │
│   ├── App.tsx                   # Root component with providers
│   ├── router.tsx                # Route definitions
│   ├── main.tsx                  # Entry point
│   └── index.css                 # Global styles & CSS variables
│
├── tailwind.config.js            # Tailwind configuration
├── components.json               # shadcn/ui configuration
├── tsconfig.json                 # TypeScript configuration
└── vite.config.ts                # Vite bundler configuration
```

## Routing

**Library:** React Router DOM v6.22.3

**Route Definitions:** `src/router.tsx`

### Main Routes

| Path | Component | Description |
|------|-----------|-------------|
| `/` | `RootLayout` | Root layout with sidebar navigation |
| `/discover` | `DiscoverPage` | Discovery/landing page |
| `/history` | `HistoryPage` | Run history |
| `/runs/:runId/*` | `RunRouter` | Individual run details |
| `/browser-sessions` | `BrowserSessions` | Browser session management |
| `/tasks` | `TasksPage` | Task list |
| `/tasks/create/:template` | `CreateNewTaskFormPage` | Create new task |
| `/tasks/:taskId` | `TaskDetails` | Task details with tabs |
| `/workflows` | `Workflows` | Workflow list |
| `/workflows/:workflowPermanentId/edit` | `WorkflowEditor` | Visual workflow editor |
| `/workflows/:workflowPermanentId/run` | `WorkflowRunParameters` | Run workflow |
| `/workflows/:workflowPermanentId/:workflowRunId` | `WorkflowRun` | Workflow run details |
| `/settings` | `Settings` | User settings |
| `/credentials` | `CredentialsPage` | Credential management |

### Routing Patterns

- Uses `createBrowserRouter` for route configuration
- Nested routes with `<Outlet />` for sub-pages
- Index routes with `<Navigate>` for default redirects
- Layout components wrap related routes

## State Management

The application uses a multi-layered state management approach:

### 1. Zustand (Client State)

Primary store pattern located in `src/store/`:

```typescript
// Example: useAutoplayStore.ts
type AutoplayStore = {
  wpid: string | null;
  blockLabel: string | null;
  setAutoplay: (wpid: string | null, blockLabel: string | null) => void;
  clearAutoplay: () => void;
};

export const useAutoplayStore = create<AutoplayStore>((set) => ({
  wpid: null,
  blockLabel: null,
  setAutoplay: (wpid, blockLabel) => set({ wpid, blockLabel }),
  clearAutoplay: () => set({ wpid: null, blockLabel: null }),
}));
```

**Key Stores:**
- `useAutoplayStore` - Autoplay workflow execution
- `useSidebarStore` - Sidebar collapse state
- `useWorkflowPanelStore` - Workflow editor panel state
- `useRecordingStore` - Recording playback state
- `useSettingsStore` - User settings
- `BlockActionContext`, `BlockOutputStore` - Workflow block data

### 2. React Context (Shared Application State)

- `ThemeProviderContext` - Dark/light/system theme
- `UserContext` - User information
- `CredentialGetterContext` - Credential fetching
- `CloudContext` - Cloud configuration
- `DebugStoreContext` - Debug mode from URL params

### 3. React Query v5.28.6 (Server State)

Configuration in `src/api/QueryClient.ts`:

```typescript
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      retry: false,
    },
  },
});
```

## Styling

### Tech Stack

| Tool | Version | Purpose |
|------|---------|---------|
| Tailwind CSS | ^3.4.17 | Utility-first CSS |
| class-variance-authority | ^0.7.0 | Component variants |
| tailwind-merge | ^2.2.2 | Smart class merging |
| clsx | ^2.1.0 | Conditional classes |
| tailwindcss-animate | ^1.0.7 | Animation utilities |

### Theme System

**Approach:** CSS Variables in HSL format with class-based dark mode

**Token Location:** `src/index.css`

```css
:root {
  --background: 0 0% 100%;
  --foreground: 222.2 84% 4.9%;
  --primary: 222.2 47.4% 11.2%;
  --secondary: 210 40% 96.1%;
  --destructive: 0 84.2% 60.2%;
  --warning: 40.6 96.1% 40.4%;
  --success: 142.1 76.2% 36.3%;
  /* ... more tokens */
}

.dark {
  --background: 222.2 84% 4.9%;
  --foreground: 210 40% 98%;
  /* ... dark overrides */

  /* Elevation system for depth */
  --slate-elevation-1: 228 45% 9%;
  --slate-elevation-2: 228 37% 10.6%;
  /* ... elevation levels 1-5 */
}
```

### CVA Component Pattern

Components use class-variance-authority for variants:

```typescript
// button-variants.ts
const buttonVariants = cva(
  "inline-flex items-center justify-center rounded-md text-sm font-medium",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary/90",
        destructive: "bg-red-900 text-destructive-foreground",
        outline: "border border-input bg-background",
        secondary: "bg-secondary text-secondary-foreground",
        ghost: "hover:bg-accent",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-md px-3 text-xs",
        lg: "h-10 rounded-md px-8",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);
```

### Utility Function

```typescript
// src/util/utils.ts
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

## API Layer

**HTTP Client:** Axios v1.8.2

**Location:** `src/api/AxiosClient.ts`

### Multiple API Clients

```typescript
const client = axios.create({ baseURL: apiV1BaseUrl });      // v1 API
const v2Client = axios.create({ baseURL: apiV2BaseUrl });    // v2 API
const clientSansApiV1 = axios.create({ baseURL: apiSansApiV1BaseUrl });
```

### Authentication

```typescript
export function setAuthorizationHeader(token: string) {
  client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
}

export function setApiKeyHeader(apiKey: string) {
  client.defaults.headers.common["X-API-Key"] = apiKey;
}
```

### Server-Sent Events

SSE support in `src/api/sse.ts` using `@microsoft/fetch-event-source`:

```typescript
export async function fetchStreamingSse<T>(
  input: RequestInfo | URL,
  init: RequestInit,
  onMessage: SseMessageHandler<T>,
  options?: SseStreamingOptions,
): Promise<void>
```

## Component Library

### UI Primitives (shadcn Pattern)

Located in `src/components/ui/`:

| Component | Base | Description |
|-----------|------|-------------|
| `button` | Native | Button with variants |
| `input` | Native | Text input |
| `card` | Native | Card container |
| `badge` | Native | Status badges |
| `select` | Radix | Dropdown select |
| `dialog` | Radix | Modal dialogs |
| `dropdown-menu` | Radix | Dropdown menus |
| `tabs` | Radix | Tab navigation |
| `tooltip` | Radix | Tooltips |
| `toast` | Radix | Notifications |
| `accordion` | Radix | Collapsible sections |
| `checkbox` | Radix | Checkbox input |
| `switch` | Radix | Toggle switch |
| `scroll-area` | Radix | Scrollable container |
| `popover` | Radix | Popover content |

### Component Pattern

```typescript
// Standard component pattern
export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";
```

## Key Dependencies

### Core Framework
- `react@^18.2.0` - React 18
- `react-dom@^18.2.0` - DOM rendering
- `react-router-dom@^6.22.3` - Routing

### State Management
- `zustand@^4.5.2` - Client state
- `@tanstack/react-query@^5.28.6` - Server state

### UI Components
- `@radix-ui/*` - 15 Radix packages for headless components
- `cmdk@^1.0.0` - Command palette

### Forms & Validation
- `react-hook-form@^7.51.1` - Form state
- `@hookform/resolvers@^3.3.4` - Validation resolvers
- `zod@^3.22.4` - Schema validation

### Code Editor
- `@uiw/react-codemirror@^4.23.0` - Code editor
- `@codemirror/lang-*` - Language support

### Workflow Visualization
- `@xyflow/react@^12.1.1` - Node-based editor
- `@dagrejs/dagre@^1.1.4` - Graph layout

### Utilities
- `axios@^1.8.2` - HTTP client
- `nanoid@^5.0.7` - ID generation
- `yaml@^2.4.2` - YAML parsing

## TypeScript Patterns

### Props with React Attributes

```typescript
export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}
```

### Enum-like Constants

```typescript
export const Status = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Completed: "completed",
  Queued: "queued",
  // ...
} as const;

export type Status = (typeof Status)[keyof typeof Status];
```

### forwardRef Pattern

```typescript
const Card = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("rounded-xl border bg-card", className)} {...props} />
));
Card.displayName = "Card";
```

## Dark Mode

**Implementation:** Class-based with system preference detection

**Provider:** `src/components/ThemeProvider.tsx`

```typescript
export type Theme = "dark" | "light" | "system";

export function ThemeProvider({ children, defaultTheme = "system" }) {
  const [theme, setTheme] = useState<Theme>(defaultTheme);

  useEffect(() => {
    const root = window.document.documentElement;
    root.classList.remove("light", "dark");

    if (theme === "system") {
      const systemTheme = window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
      root.classList.add(systemTheme);
      return;
    }

    root.classList.add(theme);
  }, [theme]);

  return (
    <ThemeProviderContext.Provider value={{ theme, setTheme }}>
      {children}
    </ThemeProviderContext.Provider>
  );
}
```

**Usage:**
```typescript
const { theme, setTheme } = useTheme();
```

## Build System

**Bundler:** Vite v5.4.21

**Scripts:**
- `npm run dev` - Development server + artifact server
- `npm run build` - TypeScript check + Vite build
- `npm run lint` - ESLint check
- `npm run format` - Prettier formatting
- `npm run test` - Vitest test runner

**TypeScript Config:**
- Target: ES2020
- Strict mode enabled
- Path alias: `@/*` → `./src/*`
