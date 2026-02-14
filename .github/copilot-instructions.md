# GitHub Copilot Repository Instructions (Skyvern)

These instructions apply to GitHub Copilot when working in this repository.

## Must-Follow Workflow (Superpowers)

- Read and follow the nearest `AGENTS.md` (agent instructions) and the repository root `CLAUDE.md`.
- If Superpowers skills are available in your environment, you MUST invoke relevant skills before responding
  or taking action. If a skill applies, you do not have a choice.

Required skills (common triggers):
- `superpowers:using-superpowers` - at the start of any task
- `superpowers:brainstorming` - before creative work / behavior changes
- `superpowers:writing-plans` - before multi-step work
- `superpowers:systematic-debugging` - before fixing bugs/test failures
- `superpowers:test-driven-development` - before implementing features/bugfixes
- `superpowers:verification-before-completion` - before claiming "done/fixed/passing"

If you cannot invoke skills in your environment, emulate the same workflow explicitly in your output:
brainstorm -> plan -> implement with TDD/debugging -> verify with commands and results.

Reference (custom instructions): https://docs.github.com/en/copilot/customizing-copilot/adding-repository-custom-instructions-for-github-copilot

