from pydantic import HttpUrl, ValidationError, parse_obj_as

from skyvern.exceptions import InvalidUrl


def validate_url(url: str) -> str:
    try:
        # Use parse_obj_as to validate the string as an HttpUrl
        parse_obj_as(HttpUrl, url)
        return url
    except ValidationError:
        # Handle the validation error
        raise InvalidUrl(url=url)
