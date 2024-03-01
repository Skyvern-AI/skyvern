import re


def remove_whitespace(string: str) -> str:
    return re.sub("[ \n\t]+", " ", string)
