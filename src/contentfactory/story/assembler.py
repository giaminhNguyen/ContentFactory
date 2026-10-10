"""Story Assembler (cài đặt ở `contentfactory.storyprose` — module dùng chung để Story Remix kiểm hợp đồng ngay khi sinh mà không import chéo package)."""
from ..storyprose import (DECOR, END_META, HEADING, MARKER, MIN_DUP_CHARS, RECAP, TERMINAL, _is_heading, _norm, assemble)  # noqa: F401
