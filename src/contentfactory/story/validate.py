"""Validator tất định cho story.txt (MODULE_CONTRACTS §2) + sanitize_prose; cài đặt ở `contentfactory.storyprose`."""
from ..storyprose import HEADER_RE, MARKER_RE, MAX_DUP_PARAGRAPH_RATIO, sanitize_prose, validate_story_text  # noqa: F401
