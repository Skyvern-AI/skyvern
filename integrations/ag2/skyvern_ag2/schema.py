from pydantic import BaseModel


class CreateTaskInput(BaseModel):
    user_prompt: str
    url: str | None = None


class GetTaskInput(BaseModel):
    task_id: str
