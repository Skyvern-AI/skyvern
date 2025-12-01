-- Supabase Database Schema for Skyvern SaaS
-- This schema manages user data, workflows, runs, and credentials

-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================
-- 1. User Profiles (extends Supabase Auth)
-- =============================================
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
    email TEXT,
    full_name TEXT,
    avatar_url TEXT,
    organization_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

-- Policies for profiles
CREATE POLICY "Users can view their own profile"
    ON public.profiles FOR SELECT
    USING (auth.uid() = id);

CREATE POLICY "Users can update their own profile"
    ON public.profiles FOR UPDATE
    USING (auth.uid() = id);

CREATE POLICY "Users can insert their own profile"
    ON public.profiles FOR INSERT
    WITH CHECK (auth.uid() = id);

-- Trigger to create profile on user signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.profiles (id, email, full_name, avatar_url)
    VALUES (
        NEW.id,
        NEW.email,
        NEW.raw_user_meta_data->>'full_name',
        NEW.raw_user_meta_data->>'avatar_url'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- =============================================
-- 2. Workflows
-- =============================================
CREATE TABLE IF NOT EXISTS public.workflows (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    permanent_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    workflow_definition JSONB NOT NULL DEFAULT '{}',
    is_public BOOLEAN DEFAULT FALSE,
    folder_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.workflows ENABLE ROW LEVEL SECURITY;

-- Policies for workflows
CREATE POLICY "Users can view their own workflows"
    ON public.workflows FOR SELECT
    USING (auth.uid() = user_id OR is_public = TRUE);

CREATE POLICY "Users can create their own workflows"
    ON public.workflows FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update their own workflows"
    ON public.workflows FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete their own workflows"
    ON public.workflows FOR DELETE
    USING (auth.uid() = user_id);

-- =============================================
-- 3. Workflow Folders
-- =============================================
CREATE TABLE IF NOT EXISTS public.workflow_folders (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Add foreign key to workflows
ALTER TABLE public.workflows
    ADD CONSTRAINT fk_workflow_folder
    FOREIGN KEY (folder_id) REFERENCES public.workflow_folders(id)
    ON DELETE SET NULL;

-- Enable RLS
ALTER TABLE public.workflow_folders ENABLE ROW LEVEL SECURITY;

-- Policies for folders
CREATE POLICY "Users can view their own folders"
    ON public.workflow_folders FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create their own folders"
    ON public.workflow_folders FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update their own folders"
    ON public.workflow_folders FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete their own folders"
    ON public.workflow_folders FOR DELETE
    USING (auth.uid() = user_id);

-- =============================================
-- 4. Workflow Runs (Execution History)
-- =============================================
CREATE TYPE run_status AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled');

CREATE TABLE IF NOT EXISTS public.workflow_runs (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    workflow_id UUID REFERENCES public.workflows(id) ON DELETE SET NULL,
    workflow_permanent_id TEXT NOT NULL,
    status run_status DEFAULT 'pending',
    parameters JSONB DEFAULT '{}',
    output JSONB DEFAULT '{}',
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.workflow_runs ENABLE ROW LEVEL SECURITY;

-- Policies for runs
CREATE POLICY "Users can view their own runs"
    ON public.workflow_runs FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create their own runs"
    ON public.workflow_runs FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update their own runs"
    ON public.workflow_runs FOR UPDATE
    USING (auth.uid() = user_id);

-- =============================================
-- 5. Saved Credentials
-- =============================================
CREATE TYPE credential_type AS ENUM ('password', 'api_key', 'oauth', 'totp');

CREATE TABLE IF NOT EXISTS public.credentials (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    name TEXT NOT NULL,
    credential_type credential_type NOT NULL,
    -- Encrypted credential data (encrypt in application layer)
    encrypted_data TEXT NOT NULL,
    domain TEXT,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.credentials ENABLE ROW LEVEL SECURITY;

-- Policies for credentials
CREATE POLICY "Users can view their own credentials"
    ON public.credentials FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create their own credentials"
    ON public.credentials FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update their own credentials"
    ON public.credentials FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete their own credentials"
    ON public.credentials FOR DELETE
    USING (auth.uid() = user_id);

-- =============================================
-- 6. Browser Sessions (for tracking)
-- =============================================
CREATE TYPE session_status AS ENUM ('active', 'idle', 'closed');

CREATE TABLE IF NOT EXISTS public.browser_sessions (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    session_id TEXT UNIQUE NOT NULL,
    status session_status DEFAULT 'active',
    browser_info JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

-- Enable RLS
ALTER TABLE public.browser_sessions ENABLE ROW LEVEL SECURITY;

-- Policies for browser sessions
CREATE POLICY "Users can view their own sessions"
    ON public.browser_sessions FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create their own sessions"
    ON public.browser_sessions FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update their own sessions"
    ON public.browser_sessions FOR UPDATE
    USING (auth.uid() = user_id);

-- =============================================
-- 7. Artifacts (Screenshots, Recordings, Logs)
-- =============================================
CREATE TYPE artifact_type AS ENUM ('screenshot', 'recording', 'log', 'file');

CREATE TABLE IF NOT EXISTS public.artifacts (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    run_id UUID REFERENCES public.workflow_runs(id) ON DELETE CASCADE,
    artifact_type artifact_type NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT,
    mime_type TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.artifacts ENABLE ROW LEVEL SECURITY;

-- Policies for artifacts
CREATE POLICY "Users can view their own artifacts"
    ON public.artifacts FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create their own artifacts"
    ON public.artifacts FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete their own artifacts"
    ON public.artifacts FOR DELETE
    USING (auth.uid() = user_id);

-- =============================================
-- Indexes for performance
-- =============================================
CREATE INDEX idx_workflows_user_id ON public.workflows(user_id);
CREATE INDEX idx_workflows_permanent_id ON public.workflows(permanent_id);
CREATE INDEX idx_workflow_runs_user_id ON public.workflow_runs(user_id);
CREATE INDEX idx_workflow_runs_workflow_id ON public.workflow_runs(workflow_id);
CREATE INDEX idx_workflow_runs_status ON public.workflow_runs(status);
CREATE INDEX idx_credentials_user_id ON public.credentials(user_id);
CREATE INDEX idx_browser_sessions_user_id ON public.browser_sessions(user_id);
CREATE INDEX idx_artifacts_run_id ON public.artifacts(run_id);

-- =============================================
-- Updated_at trigger function
-- =============================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply updated_at triggers
CREATE TRIGGER update_profiles_updated_at
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_workflows_updated_at
    BEFORE UPDATE ON public.workflows
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_workflow_folders_updated_at
    BEFORE UPDATE ON public.workflow_folders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_credentials_updated_at
    BEFORE UPDATE ON public.credentials
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
