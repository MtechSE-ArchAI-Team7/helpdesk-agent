from __future__ import annotations

import re
from urllib.parse import urlsplit


class BrowserTargetPolicy:
    @property
    def allowed_hosts(self) -> list[str] | tuple[str, ...]:
        raise NotImplementedError

    @classmethod
    def reported_urls(cls, report: str) -> list[str]:
        urls = []
        for match in cls._URL_PATTERN.findall(report):
            url = match.rstrip(cls._TRAILING_URL_PUNCTUATION)
            if url and url not in urls:
                urls.append(url)
        return urls

    def validate_target(self, target_url: str | None, report: str) -> tuple[str | None, str]:
        """Return the normalized allowed host, or a reason why navigation is denied."""
        if not target_url:
            return None, "no_target_url"
        if target_url not in self.reported_urls(report):
            return None, "target_url_not_in_user_report"

        try:
            parsed = urlsplit(target_url)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except ValueError:
            return None, "invalid_target_url"

        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return None, "http_or_https_url_required"
        allowed_ports = (None, 80) if parsed.scheme == "http" else (None, 443)
        if port not in allowed_ports:
            return None, "nonstandard_port_not_allowed"

        allowed_hosts = {host.lower().rstrip(".") for host in self.allowed_hosts}
        if host not in allowed_hosts:
            return None, "host_not_allowlisted"
        return host, "allowed"

    _URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

    _TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
