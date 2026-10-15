import re
from functools import lru_cache

NON_WORD = re.compile(r"[\W_]+")


@lru_cache(maxsize=1 << 16)
def search_key(value: str) -> str:
    folded = value.casefold()
    return (NON_WORD.sub("", folded) or folded.strip()).replace("ё", "е")
