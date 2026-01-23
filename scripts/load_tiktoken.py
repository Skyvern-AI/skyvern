import tiktoken

for model in tiktoken.model.MODEL_TO_ENCODING.keys():
    tiktoken.encoding_for_model(model)
