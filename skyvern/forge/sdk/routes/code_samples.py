RUN_TASK_CODE_SAMPLE = """from skyvern import Skyvern

client = Skyvern(api_key="your_api_key")
await client.run_task(prompt="What's the top post on hackernews?")
"""

GET_RUN_CODE_SAMPLE = """from skyvern import Skyvern

client = Skyvern(api_key="your_api_key")
run = await client.get_run(run_id="tsk_123")
print(run)
"""

RUN_WORKFLOW_CODE_SAMPLE = """from skyvern import Skyvern

client = Skyvern(api_key="your_api_key")
await client.agent.run_workflow(workflow_id="wpid_123", parameters={"parameter1": "value1", "parameter2": "value2"})
"""
