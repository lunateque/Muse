from typing import Annotated

import msgspec

from muse.hls.domain import MAX_APPEND, MAX_SONGS

type TrackId = Annotated[int, msgspec.Meta(ge=1, le=999_999_999)]


class CreateBody(msgspec.Struct):
    ids: Annotated[list[TrackId], msgspec.Meta(min_length=1, max_length=MAX_SONGS)]
    at: Annotated[float, msgspec.Meta(ge=0)]


class AppendBody(msgspec.Struct):
    ids: Annotated[list[TrackId], msgspec.Meta(min_length=1, max_length=MAX_APPEND)]
    end: bool = False
