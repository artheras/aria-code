"""Current production hosts; retired hosts must never receive account tokens."""

from urllib.parse import urlsplit

API_URL = "https://api.arthera.xyz"
SITE_URL = "https://arthera.xyz"


def current_url(url: str, *, website=False) -> str:
    value = (url or "").strip().rstrip("/")
    host = (urlsplit(value).hostname or "").lower()
    if host == "arthera.finance" or host.endswith(".arthera.finance"):
        return SITE_URL if website else API_URL
    return value
