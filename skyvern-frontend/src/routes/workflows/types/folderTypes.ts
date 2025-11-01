export interface Folder {
  folder_id: string;
  organization_id: string;
  title: string;
  description: string | null;
  workflow_count: number;
  created_at: string;
  modified_at: string;
}

export interface FolderCreate {
  title: string;
  description?: string | null;
}

export interface FolderUpdate {
  title?: string;
  description?: string | null;
}

export interface UpdateWorkflowFolderRequest {
  folder_id: string | null;
}
