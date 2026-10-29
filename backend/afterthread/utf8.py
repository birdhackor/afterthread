"""Text made encodable as UTF-8: one definition, for the services and the API schemas.

A file name read on Linux decodes with a lone surrogate for each byte that is
not UTF-8, and a JSON ``"\\ud800"`` escape loads as one. Neither encodes as
UTF-8, so a strict encode -- of a file the backend writes, or of a response
(Starlette's ``JSONResponse``) -- refuses the whole text over one character.
``llm_log`` keeps a copy of its own (``llm_log._utf8_safe``), which a test pins
equal to this one.
"""


def encodable(text: str) -> str:
    """``text`` with each lone surrogate replaced by three U+FFFD; the rest unchanged.

    Encoding with ``errors="surrogatepass"`` lets a lone surrogate through as its
    three bytes, which are not valid UTF-8, and decoding those with
    ``errors="replace"`` gives one U+FFFD per byte; the decode cannot fail. A
    plain ``encode("utf-8", errors="replace")`` would give ``?`` instead.
    """
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
