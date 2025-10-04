import typer
import json
from typing import List
from pathlib import Path

from evaluation.core.utils import WebVoyagerTestCase, load_webvoyager_case_from_json

app = typer.Typer()

@app.command()
def evaluate(
    input_path: str = typer.Argument(..., help="Path to the input JSONL file with WebVoyager tasks"),
    output_path: str = typer.Option("results.jsonl", "--out", "-o", help="Where to save the evaluation results"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run logic without simulating agent output"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print detailed results in console")
):
    tasks: List[WebVoyagerTestCase] = list(load_webvoyager_case_from_json(input_path))
    results = []

    for task in tasks:
        # Simulate evaluation logic (this would be actual agent output in real scenario)
        simulated_answer = simulate_answer(task)

        # Compare expected vs simulated (for demo, we just check keyword overlap)
        passed = evaluate_answer(task.answer, simulated_answer)

        result = {
            "id": task.id,
            "question": task.question,
            "expected_answer": task.answer,
            "simulated_answer": simulated_answer,
            "status": "PASSED" if passed else "FAILED"
        }

        if verbose:
            typer.echo(f"[{result['status']}] {task.id}: {task.question}")
        
        results.append(result)

    # Save to output_path
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    typer.echo(f"✅ Evaluation completed. {len(results)} tasks processed.")
    typer.echo(f"📁 Results saved to: {output_path}")

def simulate_answer(task: WebVoyagerTestCase) -> str:
    """Mock function to generate a fake answer."""
    # In real world: call agent/LLM/etc
    return task.question.split("?")[0] + " response"

def evaluate_answer(expected: str, actual: str) -> bool:
    """Naive evaluator: passes if keyword from expected is in actual."""
    keywords = expected.lower().split()
    for word in keywords:
        if word in actual.lower():
            return True
    return False

if __name__ == "__main__":
    app()
