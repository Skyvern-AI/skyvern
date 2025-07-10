import { useQuery } from "@tanstack/react-query";

export interface WorkflowTemplate {
  name: string;
  title: string;
  description: string;
  content: string; // Raw YAML content
}

// Default repo and directory - can be configured
const DEFAULT_REPO = "dvloper-Iustin/skyvern";
const DEFAULT_TEMPLATES_DIR = "skyvern-frontend/workflow-templates";

function getRepoUrl(): string {
  // Get from localStorage or use default
  if (typeof window !== "undefined") {
    return localStorage.getItem("workflow-templates-repo") || DEFAULT_REPO;
  }
  return DEFAULT_REPO;
}

function getTemplatesDir(): string {
  // Get from localStorage or use default
  if (typeof window !== "undefined") {
    return (
      localStorage.getItem("workflow-templates-dir") || DEFAULT_TEMPLATES_DIR
    );
  }
  return DEFAULT_TEMPLATES_DIR;
}

async function fetchWorkflowTemplates(): Promise<WorkflowTemplate[]> {
  try {
    const repo = getRepoUrl();
    const templatesDir = getTemplatesDir();

    // Fetch directory contents from GitHub API
    const response = await fetch(
      `https://api.github.com/repos/${repo}/contents/${templatesDir}`,
    );

    if (!response.ok) {
      console.warn("No workflow templates directory found");
      return [];
    }

    const files = await response.json();

    if (!Array.isArray(files)) {
      return [];
    }

    const templates: WorkflowTemplate[] = [];

    // Fetch each YAML file
    for (const file of files) {
      if (file.type === "file" && file.name.endsWith(".yaml")) {
        try {
          // Fetch raw content
          const contentResponse = await fetch(
            `https://raw.githubusercontent.com/${repo}/main/${templatesDir}/${file.name}`,
          );

          if (contentResponse.ok) {
            const content = await contentResponse.text();

            // Parse title and description from YAML
            const titleMatch = content.match(/^title:\s*(.+)$/m);
            const descriptionMatch = content.match(/^description:\s*(.+)$/m);

            const name = file.name.replace(/\.yaml$/, "");

            templates.push({
              name,
              title: titleMatch?.[1]?.trim() || name.replace(/[-_]/g, " "),
              description: descriptionMatch?.[1]?.trim() || "No description",
              content,
            });
          }
        } catch (error) {
          console.warn(`Failed to load template ${file.name}:`, error);
        }
      }
    }

    return templates;
  } catch (error) {
    console.warn("Failed to fetch workflow templates:", error);
    return [];
  }
}

export function useLocalWorkflowTemplates() {
  return useQuery<WorkflowTemplate[]>({
    queryKey: ["localWorkflowTemplates", getRepoUrl(), getTemplatesDir()],
    queryFn: fetchWorkflowTemplates,
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 30 * 60 * 1000, // 30 minutes
  });
}

// Function to update the repository
export function setWorkflowTemplatesRepo(repo: string): void {
  if (typeof window !== "undefined") {
    localStorage.setItem("workflow-templates-repo", repo);
  }
}

// Function to update the templates directory
export function setWorkflowTemplatesDir(dir: string): void {
  if (typeof window !== "undefined") {
    localStorage.setItem("workflow-templates-dir", dir);
  }
}

// Function to get current repository (for UI)
export function getCurrentRepo(): string {
  return getRepoUrl();
}

// Function to get current templates directory (for UI)
export function getCurrentTemplatesDir(): string {
  return getTemplatesDir();
}
