# Skyvern Agent Guide
This AGENTS.md file provides comprehensive guidance for AI agents working with the Skyvern codebase. Follow these guidelines to ensure consistency and quality in all contributions.

## Project Structure for Agent Navigation

- `/skyvern`: Main Python package
  - `/cli`: Command-line interface components
  - `/client`: Client implementations and integrations
  - `/forge`: Core automation logic and workflows
  - `/library`: Shared utilities and helpers
  - `/schemas`: Data models and validation schemas
  - `/services`: Business logic and service layers
  - `/utils`: Common utility functions
  - `/webeye`: Web interaction and browser automation
- `/skyvern-frontend`: Frontend application
- `/integrations`: Third-party service integrations
- `/alembic`: Database migrations
- `/scripts`: Utility and deployment scripts

## Coding Conventions for Agents

### Python Standards

- Use Python 3.11+ features and type hints
- Follow PEP 8 with a line length of 100 characters
- Use absolute imports for all modules
- Document all public functions and classes with Google-style docstrings
- Use `snake_case` for variables and functions, `PascalCase` for classes

### Asynchronous Programming

- Prefer async/await over callbacks
- Use `asyncio` for concurrency
- Always handle exceptions in async code
- Use context managers for resource cleanup

### Error Handling

- Use specific exception classes
- Include meaningful error messages
- Log errors with appropriate severity levels
- Never expose sensitive information in error messages

## Pull Request Process

1. **Branch Naming**
   - `feature/descriptive-name` for new features
   - `fix/issue-description` for bug fixes
   - `chore/task-description` for maintenance tasks

2. **PR Guidelines**
   - Reference related issues with `Fixes #123` or `Closes #123`
   - Include a clear description of changes
   - Update relevant documentation
   - Ensure all tests pass
   - Get at least one approval before merging

3. **Commit Message Format**
   ```
   [Component] Action: Brief description
   
   More detailed explanation if needed.
   
   - Bullet points for additional context
   - Reference issues with #123
   ```

## Code Quality Checks

Before submitting code, run:
```bash
pre-commit run --all-files
```

## Performance Considerations
- Optimize database queries
- Use appropriate data structures
- Implement caching where beneficial
- Monitor memory usage

## Security Best Practices
- Never commit secrets or credentials
- Validate all inputs
- Use environment variables for configuration
- Follow the principle of least privilege
- Keep dependencies updated

## Getting Help
- Check existing issues before opening new ones
- Reference relevant documentation
- Provide reproduction steps for bugs
- Be specific about the problem and expected behavior
