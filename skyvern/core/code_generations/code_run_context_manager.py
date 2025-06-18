from skyvern.core.code_generations.skyvern_page import RunContext


class CodeRunContextManager:
    """
    Manages the run context for code runs.
    """

    def __init__(self) -> None:
        self.run_contexts: dict[str, RunContext] = {}
        """
        run_id -> RunContext
        """

    def get_run_context(self, run_id: str) -> RunContext | None:
        return self.run_contexts.get(run_id)

    def set_run_context(self, run_id: str, run_context: RunContext) -> None:
        self.run_contexts[run_id] = run_context

    def delete_run_context(self, run_id: str) -> None:
        self.run_contexts.pop(run_id, None)
