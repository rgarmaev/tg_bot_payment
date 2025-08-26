from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit, unquote, quote


def sanitize_config_link(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url)
        fragment = parts.fragment or ""
        if not fragment:
            return url
        decoded = unquote(fragment)
        # Remove analytics suffix that starts with a hyphen followed by a digit (e.g., -100.00GB-29D,...)
        m = re.match(r"^(.*?)(?:-\d.*)$", decoded)
        base_fragment = m.group(1) if m else decoded
        # Normalize spaces and strip
        base_fragment = base_fragment.strip()
        # Rebuild URL with sanitized fragment
        new_fragment = quote(base_fragment, safe="-._~")
        sanitized = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, new_fragment))
        return sanitized
    except Exception:
        return url