import structlog

from skyvern.forge.prompts import prompt_engine

LOG = structlog.get_logger()

class Prompt:
    def __init__(self, name, template, score=0):
        self.name = name
        self.template = template
        self.score = score

class PromptManager:
    def __init__(self):
        self.prompts = {}
        self._load_baseline_prompt()

    def _load_baseline_prompt(self):
        """
        Loads the original 'extract-action.j2' prompt as the baseline.
        """
        try:
            # Access the Jinja2 environment from the prompt_engine
            env = prompt_engine.env
            # Construct the path to the template within the Jinja2 environment
            template_path = "skyvern/extract-action.j2"
            # Get the template source from the loader
            baseline_template = env.loader.get_source(env, template_path)[0]

            self.add_prompt("baseline", baseline_template, score=1.0) # Assuming baseline is good.
            LOG.info("Loaded baseline prompt 'extract-action.j2'.")
        except Exception as e:
            LOG.error(f"Failed to load baseline prompt: {e}", exc_info=True)

    def add_prompt(self, name, template, score=0):
        """
        Adds a new prompt to the population.
        """
        if name in self.prompts:
            LOG.warning(f"Prompt with name '{name}' already exists. Overwriting.")

        self.prompts[name] = Prompt(name, template, score)
        LOG.info(f"Added prompt '{name}' with score {score}.")

    def get_prompt(self, name):
        """
        Retrieves a prompt object by its name.
        """
        return self.prompts.get(name)

    def get_best_prompt(self):
        """
        Returns the prompt with the highest score.
        """
        if not self.prompts:
            return None

        return max(self.prompts.values(), key=lambda p: p.score)

    def update_score(self, name, score):
        """
        Updates the score of a prompt after evaluation.
        """
        if name in self.prompts:
            self.prompts[name].score = score
            LOG.info(f"Updated score for prompt '{name}' to {score}.")
        else:
            LOG.warning(f"Prompt '{name}' not found for score update.")