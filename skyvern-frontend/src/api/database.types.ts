// Supabase Database Types
// Auto-generated types based on the database schema

export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[];

export type RunStatus = "pending" | "running" | "completed" | "failed" | "cancelled";
export type CredentialType = "password" | "api_key" | "oauth" | "totp";
export type SessionStatus = "active" | "idle" | "closed";
export type ArtifactType = "screenshot" | "recording" | "log" | "file";

export interface Database {
  public: {
    Tables: {
      profiles: {
        Row: {
          id: string;
          email: string | null;
          full_name: string | null;
          avatar_url: string | null;
          organization_name: string | null;
          created_at: string;
          updated_at: string;
        };
        Insert: {
          id: string;
          email?: string | null;
          full_name?: string | null;
          avatar_url?: string | null;
          organization_name?: string | null;
          created_at?: string;
          updated_at?: string;
        };
        Update: {
          id?: string;
          email?: string | null;
          full_name?: string | null;
          avatar_url?: string | null;
          organization_name?: string | null;
          created_at?: string;
          updated_at?: string;
        };
      };
      workflows: {
        Row: {
          id: string;
          user_id: string;
          permanent_id: string;
          title: string;
          description: string | null;
          workflow_definition: Json;
          is_public: boolean;
          folder_id: string | null;
          created_at: string;
          updated_at: string;
        };
        Insert: {
          id?: string;
          user_id: string;
          permanent_id: string;
          title: string;
          description?: string | null;
          workflow_definition?: Json;
          is_public?: boolean;
          folder_id?: string | null;
          created_at?: string;
          updated_at?: string;
        };
        Update: {
          id?: string;
          user_id?: string;
          permanent_id?: string;
          title?: string;
          description?: string | null;
          workflow_definition?: Json;
          is_public?: boolean;
          folder_id?: string | null;
          created_at?: string;
          updated_at?: string;
        };
      };
      workflow_folders: {
        Row: {
          id: string;
          user_id: string;
          name: string;
          description: string | null;
          created_at: string;
          updated_at: string;
        };
        Insert: {
          id?: string;
          user_id: string;
          name: string;
          description?: string | null;
          created_at?: string;
          updated_at?: string;
        };
        Update: {
          id?: string;
          user_id?: string;
          name?: string;
          description?: string | null;
          created_at?: string;
          updated_at?: string;
        };
      };
      workflow_runs: {
        Row: {
          id: string;
          user_id: string;
          workflow_id: string | null;
          workflow_permanent_id: string;
          status: RunStatus;
          parameters: Json;
          output: Json;
          error_message: string | null;
          started_at: string | null;
          completed_at: string | null;
          created_at: string;
        };
        Insert: {
          id?: string;
          user_id: string;
          workflow_id?: string | null;
          workflow_permanent_id: string;
          status?: RunStatus;
          parameters?: Json;
          output?: Json;
          error_message?: string | null;
          started_at?: string | null;
          completed_at?: string | null;
          created_at?: string;
        };
        Update: {
          id?: string;
          user_id?: string;
          workflow_id?: string | null;
          workflow_permanent_id?: string;
          status?: RunStatus;
          parameters?: Json;
          output?: Json;
          error_message?: string | null;
          started_at?: string | null;
          completed_at?: string | null;
          created_at?: string;
        };
      };
      credentials: {
        Row: {
          id: string;
          user_id: string;
          name: string;
          credential_type: CredentialType;
          encrypted_data: string;
          domain: string | null;
          description: string | null;
          created_at: string;
          updated_at: string;
        };
        Insert: {
          id?: string;
          user_id: string;
          name: string;
          credential_type: CredentialType;
          encrypted_data: string;
          domain?: string | null;
          description?: string | null;
          created_at?: string;
          updated_at?: string;
        };
        Update: {
          id?: string;
          user_id?: string;
          name?: string;
          credential_type?: CredentialType;
          encrypted_data?: string;
          domain?: string | null;
          description?: string | null;
          created_at?: string;
          updated_at?: string;
        };
      };
      browser_sessions: {
        Row: {
          id: string;
          user_id: string;
          session_id: string;
          status: SessionStatus;
          browser_info: Json;
          started_at: string;
          last_activity_at: string;
          closed_at: string | null;
        };
        Insert: {
          id?: string;
          user_id: string;
          session_id: string;
          status?: SessionStatus;
          browser_info?: Json;
          started_at?: string;
          last_activity_at?: string;
          closed_at?: string | null;
        };
        Update: {
          id?: string;
          user_id?: string;
          session_id?: string;
          status?: SessionStatus;
          browser_info?: Json;
          started_at?: string;
          last_activity_at?: string;
          closed_at?: string | null;
        };
      };
      artifacts: {
        Row: {
          id: string;
          user_id: string;
          run_id: string | null;
          artifact_type: ArtifactType;
          file_path: string;
          file_size: number | null;
          mime_type: string | null;
          metadata: Json;
          created_at: string;
        };
        Insert: {
          id?: string;
          user_id: string;
          run_id?: string | null;
          artifact_type: ArtifactType;
          file_path: string;
          file_size?: number | null;
          mime_type?: string | null;
          metadata?: Json;
          created_at?: string;
        };
        Update: {
          id?: string;
          user_id?: string;
          run_id?: string | null;
          artifact_type?: ArtifactType;
          file_path?: string;
          file_size?: number | null;
          mime_type?: string | null;
          metadata?: Json;
          created_at?: string;
        };
      };
    };
    Enums: {
      run_status: RunStatus;
      credential_type: CredentialType;
      session_status: SessionStatus;
      artifact_type: ArtifactType;
    };
  };
}

// Convenience types
export type Profile = Database["public"]["Tables"]["profiles"]["Row"];
export type Workflow = Database["public"]["Tables"]["workflows"]["Row"];
export type WorkflowFolder = Database["public"]["Tables"]["workflow_folders"]["Row"];
export type WorkflowRun = Database["public"]["Tables"]["workflow_runs"]["Row"];
export type Credential = Database["public"]["Tables"]["credentials"]["Row"];
export type BrowserSession = Database["public"]["Tables"]["browser_sessions"]["Row"];
export type Artifact = Database["public"]["Tables"]["artifacts"]["Row"];
