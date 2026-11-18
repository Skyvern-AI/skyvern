# CrawlDex preflight and reporting

CrawlDex is a public reliability index for AI agents using websites. It helps a
Skyvern workflow check whether a task is likely to work before execution and
submit a redacted, score-neutral outcome report afterward.

Install:

```bash
pip install "crawldex-report>=0.1.1"
```

Environment:

```bash
export CRAWLDEX_REPORT_URL="https://crawldex.com/api/v1/runs"
export CRAWLDEX_API_ORIGIN="https://crawldex.com"
export CRAWLDEX_AGENT_KEY="aa_agent_..."
```

Example configuration:

```yaml
crawldex: true
site: example.com
task: jobs_careers.submit_application
```

Example helper:

```python
from crawldex_report import CrawlDexReporter
from crawldex_report.skyvern import report_skyvern_task


async def run_with_crawldex(skyvern_client, task_request, crawldex_config):
    reporter = CrawlDexReporter()

    if crawldex_config.get("crawldex"):
        preflight = await reporter.preflight(
            crawldex_config["site"],
            crawldex_config["task"],
        )
        if preflight.warning:
            print(f"CrawlDex preflight warning: {preflight.warning}")

    task_run = await skyvern_client.run_task(task_request)

    if crawldex_config.get("crawldex"):
        await report_skyvern_task(
            reporter=reporter,
            task_run=task_run,
            site=crawldex_config["site"],
            task=crawldex_config["task"],
            agent_profile={
                "stack": "skyvern",
                "browser_runtime": "chromium",
            },
            outcome="partial",
            friction=["file_upload_required", "user_review_required"],
            evidence_signals=[
                "application_form_loaded",
                "stopped_before_user_document_upload",
            ],
        )

    return task_run
```

Safety posture:

- Fail open: CrawlDex availability never blocks the Skyvern workflow.
- Reports are opt-in and redacted by default.
- Do not submit screenshots, cookies, browser storage, document names, form
  values, downloaded files, prompts containing private user instructions, or raw
  page text.
- Stop before uploads, payments, legal submissions, account changes, or final
  confirmations unless the user is present and has explicitly approved the step.
- Do not include bypass instructions for CAPTCHA, MFA, paywalls, anti-bot
  systems, or account controls.
