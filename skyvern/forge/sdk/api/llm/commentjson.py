# vendored from https://github.com/vaidik/commentjson/blob/master/commentjson/commentjson.py since that project seems to be abandoned.

import codecs
import json
import traceback
from typing import Any, TypeVar

import lark
from lark import Lark
from lark.lexer import Token
from lark.reconstruct import Reconstructor
from lark.tree import Tree

parser = Lark(
    """
    ?start: value
    ?value: object
          | array
          | string
          | SIGNED_NUMBER      -> number
          | "true"             -> true
          | "false"            -> false
          | "null"             -> null
    array  : "[" [value ("," value)*] TRAILING_COMMA? "]"
    object : "{" [pair ("," pair)*]  TRAILING_COMMA? "}"
    pair   : string ":" value
    string : ESCAPED_STRING

    COMMENT: /(#|\\/\\/)[^\\n]*/
    TRAILING_COMMA: ","

    %import common.ESCAPED_STRING
    %import common.SIGNED_NUMBER
    %import common.WS
    %ignore WS
    %ignore COMMENT
""",
    maybe_placeholders=False,
    parser="lalr",
)

serializer = Reconstructor(parser)


def detect_encoding(b: bytes) -> str:
    """
    Taken from `json` package in CPython 3.7.

    Source can be found at https://bit.ly/2OHqCIK.
    """

    bstartswith = b.startswith
    if bstartswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
        return "utf-32"
    if bstartswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        return "utf-16"
    if bstartswith(codecs.BOM_UTF8):
        return "utf-8-sig"

    if len(b) >= 4:
        if not b[0]:
            # 00 00 -- -- - utf-32-be
            # 00 XX -- -- - utf-16-be
            return "utf-16-be" if b[1] else "utf-32-be"
        if not b[1]:
            # XX 00 00 00 - utf-32-le
            # XX 00 00 XX - utf-16-le
            # XX 00 XX -- - utf-16-le
            return "utf-16-le" if b[2] or b[3] else "utf-32-le"
    elif len(b) == 2:
        if not b[0]:
            # 00 XX - utf-16-be
            return "utf-16-be"
        if not b[1]:
            # XX 00 - utf-16-le
            return "utf-16-le"
    # default
    return "utf-8"


class BaseException(Exception):
    """Base exception to be implemented and raised while handling exceptions
    raised by libraries used in `commentjson`.

    Sets message of self in a way that it clearly calls out that the exception
    was raised by another library, along with the entire stacktrace of the
    exception raised by the other library.
    """

    library: str | None = None
    message: str

    def __init__(self, exc: Exception) -> None:
        if self.library is None:
            raise NotImplementedError("Value of library must be set in the inherited exception class.")

        tb = traceback.format_exc()
        tb = "\n".join(" " * 4 + line_ for line_ in tb.split("\n"))

        error = getattr(exc, "msg", None) or getattr(exc, "message", None) or str(exc)
        self.message = "\n".join(
            [
                "JSON Library Exception\n",
                ("Exception thrown by library ({}): \033[4;37m{}\033[0m\n".format(self.library, error)),
                "%s" % tb,
            ]
        )
        Exception.__init__(self, self.message)


class ParserException(BaseException):
    """Exception raised when the `lark` raises an exception i.e.
    the exception is not caused by `commentjson` and caused by the use of
    `lark` in `commentjson`.
    """

    library = "lark"


class JSONLibraryException(BaseException):
    """Exception raised when the `json` raises an exception i.e.
    the exception is not caused by `commentjson` and caused by the use of
    `json` in `commentjson`.

    .. note::

        As of now, ``commentjson`` supports only standard library's ``json``
        module. It might start supporting other widely-used contributed JSON
        libraries in the future.
    """

    library = "json"


T = TypeVar("T", Tree, Token)


def _remove_trailing_commas(tree: T) -> T:
    if isinstance(tree, Tree):
        tree.children = [
            _remove_trailing_commas(ch)
            for ch in tree.children
            if not (isinstance(ch, Token) and ch.type == "TRAILING_COMMA")
        ]
    return tree


def loads(text: str | bytes | bytearray, *args: Any, **kwargs: Any) -> Any:
    """Deserialize `text` (a `str` or `unicode` instance containing a JSON
    document with Python or JavaScript like comments) to a Python object.

    :param text: serialized JSON string with or without comments.
    :param kwargs: all the arguments that `json.loads <http://docs.python.org/
                   2/library/json.html#json.loads>`_ accepts.
    :returns: dict or list.
    """

    if isinstance(text, (bytes, bytearray)):
        text = text.decode(detect_encoding(text), "surrogatepass")

    try:
        parsed = _remove_trailing_commas(parser.parse(text))
        final_text = serializer.reconstruct(parsed)
    except lark.exceptions.UnexpectedCharacters:
        raise ValueError("Unable to parse text", text)

    return json.loads(final_text, *args, **kwargs)
