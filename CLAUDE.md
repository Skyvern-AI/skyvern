# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Python Backend Commands
- **Install dependencies**: `uv sync`
- **Run Skyvern service**: `skyvern run all` (starts both backend and UI)
- **Run backend only**: `skyvern run server`
- **Run UI only**: `skyvern run ui`
- **Check status**: `skyvern status`
- **Stop services**: `skyvern stop all`
- **Quickstart**: `skyvern quickstart` (for first-time setup with DB migrations)

### Code Quality & Testing
- **Lint**: `ruff check` and `ruff format`
- **Type checking**: `mypy skyvern`
- **Run tests**: `pytest tests/`
- **Pre-commit hooks**: `pre-commit run --all-files`

### Frontend Commands (in skyvern-frontend/)
- **Install dependencies**: `npm install`
- **Development**: `npm run dev`
- **Build**: `npm run build`
- **Lint**: `npm run lint`
- **Format**: `npm run format`

### Database Management
- **Run migrations**: `alembic upgrade head`
- **Create migration**: `alembic revision --autogenerate -m "description"`

## Architecture Overview

Skyvern is a browser automation platform that uses LLMs and computer vision to interact with websites. The architecture consists of:

### Core Components
- **Agent System** (`skyvern/agent/`): Multi-agent system for web navigation and task execution
- **Browser Engine** (`skyvern/webeye/`): Playwright-based browser automation with computer vision
- **Workflow Engine** (`skyvern/services/`): Orchestrates complex multi-step workflows
- **API Layer** (`skyvern/forge/`): FastAPI-based REST API and WebSocket support

### Key Directories
- `skyvern/forge/agent.py`: ForgeAgent class - main execution loop for tasks
- `skyvern/webeye/`: Browser automation, DOM scraping, action execution
    - `actions/`: Action types (click, fill, extract, etc.) and execution handlers
    - `scraper/`: DOM scraping and element tree building
- `skyvern/forge/sdk/`: Core SDK components
    - `workflow/`: Workflow definitions, blocks, context management
    - `routes/`: API endpoints
    - `db/`: Database models and ORM client
    - `api/llm/`: Multi-provider LLM integration
    - `artifact/`: Artifact storage abstraction (local/S3/Azure)
- `skyvern/services/`: Business logic services
    - `task_v2_service.py`: TaskV2 execution (preferred for new tasks)
    - `workflow_service.py`: Workflow orchestration
    - `script_service.py`: Script generation and execution
- `skyvern/client/`: Generated Python client SDK
- `skyvern-frontend/`: React-based UI for task management and monitoring
- `alembic/`: Database migrations

### Multi-LLM Architecture

Skyvern uses specialized LLM handlers for different purposes (configured in `skyvern/forge/forge_app.py`):

- **LLM_API_HANDLER**: Main reasoning and action planning
- **SELECT_AGENT_LLM_API_HANDLER**: Element selection on web pages
- **EXTRACTION_LLM_API_HANDLER**: Data extraction from pages
- **SCRIPT_GENERATION_LLM_API_HANDLER**: Python code generation
- **CHECK_USER_GOAL_LLM_API_HANDLER**: Goal verification and completion checking
- **UI_TARS_CLIENT**: Computer vision model for element detection

This multi-LLM approach allows using different models optimized for specific tasks (e.g., cheaper models for simple selections, stronger models for complex reasoning).

### Task Execution System

**TaskV2 vs Legacy Tasks:**
- **TaskV2** (preferred): Modern system with mini-goal decomposition, better looping support, URL state management
- **Legacy Tasks**: Older task model, still supported for backward compatibility

**Key Entry Points:**
- `skyvern/services/task_v2_service.py`: TaskV2 creation and execution
- `skyvern/forge/agent.py:execute_step()`: Main execution loop

### Workflow System

**Block-Based Architecture:**
Workflows are composed of modular blocks that execute sequentially or in loops. Each block has:
- **output_parameter**: Name to store block results in workflow context
- **continue_on_failure**: Whether to continue execution if block fails
- **Parameter Passing**: Blocks use Jinja2 templating to reference previous block outputs
  - Example: `{{ ctx.previous_block_output.field_name }}`

**Block Types:**
1. **Task Blocks**: NavigationBlock, ActionBlock, ExtractionBlock, LoginBlock, FileDownloadBlock, ValidationBlock
2. **Control Flow**: ForLoopBlock, CodeBlock, TextPromptBlock, WaitBlock
3. **Data Processing**: FileParserBlock, PDFParserBlock, HttpRequestBlock, SendEmailBlock
4. **Storage**: UploadToS3Block, DownloadToS3Block, FileUploadBlock

**Important Files:**
- `skyvern/forge/sdk/workflow/models/block.py`: Block type definitions
- `skyvern/forge/sdk/workflow/context_manager.py`: Workflow execution context
- `skyvern/forge/sdk/workflow/service.py`: Workflow orchestration

### Browser Automation Layer

**Browser Session Management:**
- **Ephemeral Sessions**: Created per task, destroyed after completion
- **Persistent Sessions**: Maintained across multiple tasks (useful for authenticated workflows)
- **CDP Connection**: Can connect to existing Chrome instances via Chrome DevTools Protocol

**Browser Configuration:**
- `BROWSER_TYPE`: "chromium-headful", "chromium-headless", or "cdp-connect"
- `CHROME_EXECUTABLE_PATH`: Path to Chrome binary (for CDP connection)
- Configured in `.env` or via environment variables

**Action Execution Pipeline:**
1. ForgeAgent takes screenshot and scrapes DOM (`skyvern/webeye/scraper/scraper.py`)
2. LLM analyzes screenshot and DOM, outputs action plan
3. Actions parsed into Action objects (`skyvern/webeye/actions/parse_actions.py`)
4. ActionHandler executes actions on browser (`skyvern/webeye/actions/handler.py`)
5. ActionResults collected and stored as artifacts

### Data Flow
1. User creates tasks/workflows via UI or API
2. Agent system plans actions using LLM analysis of screenshots
3. Browser engine executes actions via Playwright
4. Results are captured, processed, and stored
5. Workflow orchestrator manages multi-step sequences

## Git Branch Naming Convention

All branches MUST follow this format:

`<type>/SKY-<issue-number>/<short-description>`

### Branch Types

The following categories are best practice and useful at the scope of an entire Pull Request:
- `fix`: A bugfix
- `feat`: A new feature
- `chore`: A general chore/tech debt change

More granular options are also useful, usually for individual commits:
- `test`: Adds tests
- `docs`: Adds/updates documentation
- `refactor`: Does not change functionality, just implementation
- `spike`: A research spike or hackathon-like task
- `build`: Related to building the project
- `ci`: Related to Github Actions/CICD
- `style`: Updating the style/formatting
- `perf`: Improving the performance of code

### Examples
- `fix/SKY-12/captcha-timeout-handling`
- `feat/SKY-4/thumbs-up-down-feedback`
- `chore/SKY-15/cleanup-deprecated-endpoints`
- `refactor/SKY-20/extract-browser-session-logic`
- `spike/SKY-25/evaluate-new-llm-provider`

### Rules
- Always derive the issue identifier from the Linear issue (e.g., SKY-4)
- Use lowercase kebab-case for the short description
- Never use generic branch names like `feature-branch` or `my-changes`

## Commit Message Convention

All commit messages MUST follow this format:

`[SKY-<issue-number>] <short-description>`

### Examples
- `[SKY-4] add thumbs up/down feedback buttons to run page`
- `[SKY-4] wire up feedback API endpoint`
- `[SKY-12] fix captcha timeout race condition`

### Rules
- Always include the Linear issue identifier in square brackets
- Use lowercase for the description (no capital first letter)
- Keep the description concise and imperative ("add", "fix", "update", not "added", "fixed", "updated")
- One logical change per commit
- No co-authors

## Coding Conventions for Agents

### Superpowers Skills (Must Use)

<EXTREMELY_IMPORTANT>
If Superpowers is available in your agent, you MUST invoke relevant skills before responding or taking
action. If a skill applies, you do not have a choice.
</EXTREMELY_IMPORTANT>

Required skills (common triggers):
- `superpowers:using-superpowers` - at the start of any task
- `superpowers:brainstorming` - before creative work / behavior changes
- `superpowers:writing-plans` - before multi-step work
- `superpowers:systematic-debugging` - before fixing bugs/test failures
- `superpowers:test-driven-development` - before implementing features/bugfixes
- `superpowers:verification-before-completion` - before claiming "done/fixed/passing"

Invocation examples (by tool):
- Claude Code: `/superpowers:brainstorm`, `/superpowers:write-plan`, `/superpowers:execute-plan` (or invoke via Skill tool: `superpowers:brainstorming`) - https://code.claude.com/docs/en/skills
- Codex: `~/.codex/superpowers/.codex/superpowers-codex find-skills`, `~/.codex/superpowers/.codex/superpowers-codex bootstrap`, `~/.codex/superpowers/.codex/superpowers-codex use-skill superpowers:brainstorming` - https://github.com/obra/superpowers/blob/main/docs/README.codex.md
- GitHub Copilot coding agent: reads `**/AGENTS.md`; enforce these phases in your response/plan and (if needed) in PR comments (e.g. `@copilot start with superpowers:brainstorming`) - https://docs.github.com/en/copilot/customizing-copilot/adding-repository-custom-instructions-for-github-copilot


## Development Notes

### Environment Setup
- Requires Python 3.11+ and Node.js
- Uses UV for Python dependency management
- PostgreSQL database (managed via Docker or local install)
- Browser dependencies installed via Playwright

### LLM Configuration
Configure via environment variables or `skyvern init llm`:
- Supports OpenAI, Anthropic, Azure OpenAI, AWS Bedrock, Gemini, Ollama
- Uses `LLM_KEY` to specify which model to use
- `SECONDARY_LLM_KEY` for lightweight agent operations

### Testing Strategy
- Unit tests in `tests/unit_tests/`
- Integration tests require browser automation setup
- Use `pytest` with async support for testing

### Code Style
- Python: Ruff for linting and formatting (configured in pyproject.toml)
- TypeScript: ESLint + Prettier (configured in skyvern-frontend/)
- Line length: 120 characters
- Use type hints and async/await patterns