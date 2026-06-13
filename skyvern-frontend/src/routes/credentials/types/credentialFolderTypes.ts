export interface CredentialFolder {
  folder_id: string;
  organization_id: string;
  title: string;
  description: string | null;
  credential_count: number;
  created_at: string;
  modified_at: string;
}

export interface CredentialFolderCreate {
  title: string;
  description?: string | null;
}

export interface CredentialFolderUpdate {
  title?: string;
  description?: string | null;
}

export interface UpdateCredentialFolderRequest {
  folder_id: string | null;
}
