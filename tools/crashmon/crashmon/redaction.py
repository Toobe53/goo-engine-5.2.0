"""Best-effort redaction of user, home-directory, and host identifiers.

Redaction is a privacy *reduction* measure, not a guarantee. Binary crash dumps
and process cores are copied as-is and may still contain these identifiers in
memory. The documentation calls this out explicitly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from .util import get_hostname, get_username


HOME_TOKEN = "<HOME>"
USER_TOKEN = "<USER>"
HOST_TOKEN = "<HOSTNAME>"
EMAIL_TOKEN = "<EMAIL>"
IP_TOKEN = "<IP>"
EXTRA_TOKEN = "<REDACTED>"

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")

# Avoid replacing short identifiers inside unrelated words. Paths are replaced
# literally before these patterns run, so `C:\Users\alice` is handled first.
_USER_RE_TEMPLATE = r"(?<![A-Za-z0-9_.-]){}(?![A-Za-z0-9_.-])"
_HOST_RE_TEMPLATE = r"(?<![A-Za-z0-9-]){}(?![A-Za-z0-9-])"


class Redactor:
    """Replace configured identifiers with stable ASCII placeholders.

    Args:
        level: ``none``, ``basic`` (default) or ``strict``.
        username/home/hostname: overrides, primarily for tests.
        extra_values: additional literal values to redact (``<REDACTED>``).
    """

    LEVELS = ("none", "basic", "strict")

    def __init__(
        self,
        level: str = "basic",
        *,
        username: Optional[str] = None,
        home: Optional[os.PathLike[str] | str] = None,
        hostname: Optional[str] = None,
        extra_values: Optional[Iterable[str]] = None,
    ) -> None:
        if level not in self.LEVELS:
            raise ValueError("unknown redaction level: {!r}".format(level))
        self.level = level
        self.replacements = 0
        self._rules: list[dict[str, Any]] = []

        if level == "none":
            return

        resolved_username = get_username() if username is None else username
        resolved_hostname = get_hostname() if hostname is None else hostname
        if home is not None:
            resolved_home = Path(home)
        else:
            try:
                resolved_home = Path.home()
            except RuntimeError:  # pragma: no cover - no home directory
                resolved_home = Path("/")

        literal_rules: list[tuple[str, str]] = []
        home_text = str(resolved_home)
        if home_text and home_text not in ("/", "\\") and len(home_text) > 2:
            variants = {home_text}
            variants.add(home_text.replace("\\", "/"))
            variants.add(home_text.replace("/", "\\"))
            for variant in sorted(variants, key=len, reverse=True):
                literal_rules.append((variant, HOME_TOKEN))

        if resolved_username and len(resolved_username) >= 2:
            literal_rules.append((resolved_username, USER_TOKEN))
        if resolved_hostname and len(resolved_hostname) >= 2:
            literal_rules.append((resolved_hostname, HOST_TOKEN))
        for extra in extra_values or ():
            if extra and len(extra) >= 2:
                literal_rules.append((extra, EXTRA_TOKEN))

        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for value, token in literal_rules:
            key = (value, token)
            if key in seen or value == token:
                continue
            seen.add(key)
            deduped.append(key)
        # Longest first so the home path wins over the bare username.
        deduped.sort(key=lambda item: len(item[0]), reverse=True)

        for value, token in deduped:
            if token == HOST_TOKEN:
                pattern = re.compile(_HOST_RE_TEMPLATE.format(re.escape(value)))
                self._rules.append({"kind": "regex", "regex": pattern, "placeholder": token})
            elif token == USER_TOKEN:
                pattern = re.compile(_USER_RE_TEMPLATE.format(re.escape(value)))
                self._rules.append({"kind": "regex", "regex": pattern, "placeholder": token})
            else:
                self._rules.append({"kind": "literal", "value": value, "placeholder": token})

        if level == "strict":
            # Run the generic patterns before identifier rules so an address
            # like alice@example.com becomes <EMAIL>, not <USER>@example.com.
            self._rules.insert(0, {"kind": "regex", "regex": _EMAIL_RE, "placeholder": EMAIL_TOKEN})
            self._rules.insert(0, {"kind": "regex", "regex": _IPV4_RE, "placeholder": IP_TOKEN})

    @property
    def applied(self) -> bool:
        return self.level != "none"

    @property
    def placeholders(self) -> list[str]:
        tokens = [rule["placeholder"] for rule in self._rules if rule["kind"] == "literal"]
        if self.level == "strict":
            tokens.extend([EMAIL_TOKEN, IP_TOKEN])
        return sorted(set(tokens))

    def redact(self, text: str) -> str:
        if not isinstance(text, str) or not text or self.level == "none":
            return text
        redacted = text
        for rule in self._rules:
            if rule["kind"] == "literal":
                count = redacted.count(rule["value"])
                if count:
                    redacted = redacted.replace(rule["value"], rule["placeholder"])
                    self.replacements += count
            else:
                redacted, count = rule["regex"].subn(
                    lambda _match, token=rule["placeholder"]: token, redacted
                )
                if count:
                    self.replacements += count
        return redacted

    def redact_obj(self, value: Any) -> Any:
        """Recursively redact string values (dict keys are schema names)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Path):
            return self.redact(str(value))
        if isinstance(value, dict):
            return {key: self.redact_obj(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_obj(item) for item in value]
        return value

    def summary(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "applied": self.applied,
            "replacements": self.replacements,
            "placeholders": self.placeholders,
        }


def redact_value(level: str, text: str) -> str:
    """Convenience helper for one-off values (used by tests/docs examples)."""
    return Redactor(level).redact(text)
