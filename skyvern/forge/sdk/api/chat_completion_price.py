from typing import Callable

from pydantic import BaseModel

openai_model_to_price_lambdas = {
    "gpt-4-vision-preview": (0.01, 0.03),
    "gpt-4-1106-preview": (0.01, 0.03),
    "gpt-3.5-turbo": (0.001, 0.002),
    "gpt-3.5-turbo-1106": (0.001, 0.002),
}


class ChatCompletionPrice(BaseModel):
    input_token_count: int
    output_token_count: int
    openai_model_to_price_lambda: Callable[[int, int], float]

    def __init__(self, input_token_count: int, output_token_count: int, model_name: str):
        input_token_price, output_token_price = openai_model_to_price_lambdas[model_name]
        super().__init__(
            input_token_count=input_token_count,
            output_token_count=output_token_count,
            openai_model_to_price_lambda=lambda input_token, output_token: input_token_price * input_token / 1000
            + output_token_price * output_token / 1000,
        )
