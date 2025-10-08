import asyncio
import structlog

from skyvern.evolution.evolve import Evolve
from skyvern.evolution.prompt_manager import PromptManager

LOG = structlog.get_logger()

async def main():
    """
    Main function to run the prompt evolution loop.
    """
    LOG.info("Initializing prompt evolution process...")

    prompt_manager = PromptManager()
    evolver = Evolve(prompt_manager)

    # Check if the baseline prompt was loaded correctly
    if not prompt_manager.get_prompt("baseline"):
        LOG.error("Failed to load baseline prompt. Aborting evolution process.")
        return

    LOG.info("Starting evolution loop...")

    # Run the evolution loop for a few generations as a demonstration
    num_generations = 5
    for i in range(num_generations):
        LOG.info(f"--- Generation {i+1}/{num_generations} ---")

        # Evolve the prompts to create new variations
        await evolver.evolve_prompts()

        # Evaluate the performance of the new prompts
        evolver.evaluate_and_score_prompts()

        # Log the best prompt of the current generation
        best_prompt = prompt_manager.get_best_prompt()
        if best_prompt:
            LOG.info(f"Best prompt of generation {i+1}: '{best_prompt.name}' with score {best_prompt.score}")
        else:
            LOG.warning("No prompts in manager after evolution and evaluation.")

        # In a real application, you might add a delay or run this as a continuous background process
        await asyncio.sleep(5)

    LOG.info("Evolution loop finished.")

if __name__ == "__main__":
    # This script needs to be run in an environment where the skyvern package is installed
    # and the necessary configurations (like .env for LLM providers) are set up.
    # Example: poetry run python scripts/run_evolution.py
    asyncio.run(main())