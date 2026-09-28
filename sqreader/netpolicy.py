"""Outbound network policy: what this process may talk to.

The upstream reader can reach a central platform (enroll, push, check-in,
offset self-heal, self-update) and a chat webhook. This fork must run with NO
runtime dependency on any external service, so every code path that opens an
outbound connection calls :func:`check_outbound` first.

Two levels:

``local-only`` (DEFAULT)
    Only loopback (127.0.0.0/8, ::1, ``localhost``), RFC1918 private ranges,
    CGNAT (100.64.0.0/10) and unique-local IPv6 (fc00::/7) are reachable. A
    domain name is reachable ONLY when it is listed verbatim in ``allow_hosts``
    (no wildcard, no implied subdomains). The check is purely lexical: it runs
    BEFORE any DNS lookup or socket, and it never resolves a name, so a hostname
    can not be smuggled past it by what it happens to resolve to.

``allow-central``
    Upstream behaviour. Nothing is blocked. Only chosen explicitly.

Where the level comes from, strongest first: an explicit ``policy=`` argument,
the ``SQREADER_NET_POLICY`` environment variable, :func:`configure` (the config
file), then the built-in default (``local-only``). ``allow_hosts`` follows the
same order (``SQREADER_NET_ALLOW_HOSTS`` is comma-separated). An unrecognised
environment value fails CLOSED to ``local-only``.

Every blocked attempt is logged as one line (purpose + host only: never the
path, query string, token or password of the URL, since a webhook URL is a
credential) and counted; see :func:`blocked_attempts` and :func:`status`.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import urllib.request
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

log = logging.getLogger("sqreader.netpolicy")

LOCAL_ONLY = "local-only"
ALLOW_CENTRAL = "allow-central"
POLICIES = (LOCAL_ONLY, ALLOW_CENTRAL)
DEFAULT_POLICY = LOCAL_ONLY

ENV_POLICY = "SQREADER_NET_POLICY"
ENV_ALLOW_HOSTS = "SQREADER_NET_ALLOW_HOSTS"

_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8",       # loopback
    "10.0.0.0/8",        # RFC1918
    "172.16.0.0/12",     # RFC1918
    "192.168.0.0/16",    # RFC1918
    "100.64.0.0/10",     # CGNAT (Tailscale and friends)
    "::1/128",           # loopback
    "fc00::/7",          # unique local
))

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address


class OutboundBlocked(Exception):
    """The network policy forbids this outbound connection.

    Raised BEFORE any DNS lookup or socket is opened. ``purpose`` and ``host``
    are safe to log (no path/query/credentials).
    """

    def __init__(self, purpose: str, host: str, reason: str) -> None:
        self.purpose = purpose
        self.host = host
        self.reason = reason
        super().__init__(
            f"dış ağ çağrısı engellendi (yerel mod): amaç={purpose} host={host}")


# -- configuration state -----------------------------------------------------

_lock = threading.Lock()
_configured_policy: Optional[str] = None
_configured_hosts: Optional[frozenset[str]] = None
_blocked_total = 0
_blocked_by_purpose: dict[str, int] = {}
_warned_bad_env = False


def configure(*, policy: Optional[str] = None,
              allow_hosts: None | str | Iterable[str] = None) -> None:
    """Set the config-file level of the policy (the env var still wins).

    ``policy=None`` / ``allow_hosts=None`` clear that setting. Raises
    ``ValueError`` for an unknown policy (a typo must not silently open the
    network, nor silently keep it shut without saying so).
    """
    global _configured_policy, _configured_hosts
    pol = None if policy is None else _norm_policy(policy)
    if policy is not None and pol is None:
        raise ValueError(
            f"bilinmeyen ağ politikası: {policy!r} (geçerli: {', '.join(POLICIES)})")
    hosts = None if allow_hosts is None else _canon_set(allow_hosts)
    with _lock:
        _configured_policy = pol
        _configured_hosts = hosts


def reset() -> None:
    """Forget configure() and zero the counters. For tests."""
    global _configured_policy, _configured_hosts, _blocked_total, _warned_bad_env
    with _lock:
        _configured_policy = None
        _configured_hosts = None
        _blocked_total = 0
        _blocked_by_purpose.clear()
        _warned_bad_env = False


def _norm_policy(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    return v if v in POLICIES else None


def effective_policy(explicit: Optional[str] = None) -> str:
    """The effective policy level."""
    global _warned_bad_env
    if explicit is not None:
        p = _norm_policy(explicit)
        if p is None:
            raise ValueError(f"bilinmeyen ağ politikası: {explicit!r}")
        return p
    env = os.environ.get(ENV_POLICY)
    if env is not None and env.strip():
        p = _norm_policy(env)
        if p is not None:
            return p
        if not _warned_bad_env:
            _warned_bad_env = True
            log.warning("%s geçersiz; '%s' kullanılıyor", ENV_POLICY, LOCAL_ONLY)
        return LOCAL_ONLY
    with _lock:
        if _configured_policy is not None:
            return _configured_policy
    return DEFAULT_POLICY


def allowed_hosts(explicit: None | str | Iterable[str] = None) -> frozenset[str]:
    """The effective allow list, canonicalised."""
    if explicit is not None:
        return _canon_set(explicit)
    env = os.environ.get(ENV_ALLOW_HOSTS)
    if env is not None and env.strip():
        return _canon_set(env)
    with _lock:
        if _configured_hosts is not None:
            return _configured_hosts
    return frozenset()


def _canon_set(hosts: str | Iterable[str]) -> frozenset[str]:
    items = hosts.split(",") if isinstance(hosts, str) else list(hosts)
    out: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        host, _ = _extract_host(item.strip())
        if host is None:
            log.warning("izin listesinde geçersiz giriş yok sayıldı")
            continue
        canon = _canonical_host(host)
        if canon is not None:
            out.add(canon)
    return frozenset(out)


# -- classification (pure, lexical: no DNS, no sockets) ----------------------

_NUMLABEL = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")


def _extract_host(target: str) -> tuple[Optional[str], str]:
    """``(host, reason)``. ``host`` is None when the target is unacceptable.

    Accepts a URL, ``host``, ``host:port`` or a bare/bracketed IPv6 literal.
    Rejects credentials, backslashes, whitespace/control characters and
    anything urllib and a browser could parse differently.
    """
    if not isinstance(target, str) or not target.strip():
        return None, "empty"
    s = target.strip()
    if any(ord(c) <= 0x20 or ord(c) == 0x7F or c.isspace() for c in s):
        return None, "control_char"
    if "\\" in s:
        return None, "backslash"
    if "://" not in s and not s.startswith("//"):
        if s.count(":") >= 2 and "[" not in s and "/" not in s:
            s = f"//[{s}]"             # bare IPv6 literal
        else:
            s = "//" + s
    try:
        parts = urlsplit(s)
        netloc = parts.netloc
        if "@" in netloc or parts.username is not None or parts.password is not None:
            return None, "userinfo"
        _ = parts.port                 # ValueError on a malformed port
        host = parts.hostname
    except ValueError:
        return None, "malformed"
    if not host:
        return None, "empty_host"
    return host, ""


def _inet_aton(labels: list[str]) -> Optional[int]:
    """Legacy IPv4 parsing (1-4 parts, decimal / 0x hex / leading-0 octal)."""
    if not 1 <= len(labels) <= 4:
        return None
    nums: list[int] = []
    for lab in labels:
        try:
            if lab[:2].lower() == "0x":
                if len(lab) == 2:
                    return None
                nums.append(int(lab, 16))
            elif len(lab) > 1 and lab[0] == "0":
                nums.append(int(lab, 8))
            else:
                nums.append(int(lab, 10))
        except ValueError:
            return None
    *head, last = nums
    if any(n > 255 for n in head):
        return None
    if last >= 256 ** (5 - len(nums)):
        return None
    value = last
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    return value


def _parse_host(host: str) -> str | _IP | None:
    """Normalise a host: an address object for IP literals (incl. the odd
    decimal/hex spellings), a lowercase ASCII string for a domain, ``None``
    when it can not be classified safely."""
    h = host.strip().lower()
    if h.endswith("."):
        h = h[:-1]
    if not h or h.endswith("."):
        return None
    if ":" in h:                                   # IPv6 (maybe with a zone id)
        try:
            return ipaddress.ip_address(h.split("%", 1)[0])
        except ValueError:
            return None
    labels = h.split(".")
    if all(_NUMLABEL.fullmatch(x) for x in labels):
        val = _inet_aton(labels)                   # numeric host is NEVER a domain
        return ipaddress.IPv4Address(val) if val is not None else None
    try:
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return h.lower()


def _unmap(ip: _IP) -> _IP:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _canonical_host(host: str) -> Optional[str]:
    parsed = _parse_host(host)
    if parsed is None:
        return None
    if isinstance(parsed, str):
        return parsed
    return str(_unmap(parsed))


def _is_local_ip(ip: _IP) -> bool:
    ip = _unmap(ip)
    return any(ip.version == n.version and ip in n for n in _LOCAL_NETS)


def _evaluate(target: Any, pol: str,
              allow: frozenset[str]) -> tuple[bool, str, str]:
    """``(allowed, host_for_log, reason)``."""
    if pol == ALLOW_CENTRAL:
        return True, "", ""
    host, why = _extract_host(target) if isinstance(target, str) else (None, "type")
    if host is None:
        return False, "<geçersiz>", why
    parsed = _parse_host(host)
    if parsed is None:
        return False, _log_safe(host), "unparseable"
    if isinstance(parsed, str):                    # a domain name
        if parsed == "localhost":
            return True, parsed, ""
        if parsed in allow:
            return True, parsed, ""
        return False, _log_safe(parsed), "domain_not_allowed"
    ip = _unmap(parsed)
    if _is_local_ip(ip) or str(ip) in allow:
        return True, str(ip), ""
    return False, str(ip), "public_ip"


def _log_safe(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9.:_\-\[\]]", "?", host)[:100]


def _log_purpose(purpose: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:\-]", "?", str(purpose))[:40] or "?"


# -- public API --------------------------------------------------------------

def is_allowed(url_or_host: Any, *, policy: Optional[str] = None,   # noqa: A002
               allow_hosts: None | str | Iterable[str] = None) -> bool:
    """Side-effect free variant of :func:`check_outbound` (no log, no counter)."""
    ok, _, _ = _evaluate(url_or_host, effective_policy(policy),
                        allowed_hosts(allow_hosts))
    return ok


def check_outbound(url_or_host: Any, *, purpose: str,
                   policy: Optional[str] = None,          # noqa: A002
                   allow_hosts: None | str | Iterable[str] = None) -> None:
    """Raise :class:`OutboundBlocked` unless connecting to ``url_or_host`` is
    permitted. Call this BEFORE resolving or connecting."""
    global _blocked_total
    pol = effective_policy(policy)
    ok, host, reason = _evaluate(url_or_host, pol, allowed_hosts(allow_hosts))
    if ok:
        return
    purpose = _log_purpose(purpose)
    with _lock:
        _blocked_total += 1
        _blocked_by_purpose[purpose] = _blocked_by_purpose.get(purpose, 0) + 1
    log.warning("dış ağ çağrısı engellendi (yerel mod): amaç=%s host=%s neden=%s",
                purpose, host, reason)
    raise OutboundBlocked(purpose, host, reason)


def is_local_only() -> bool:
    return effective_policy() == LOCAL_ONLY


def blocked_attempts() -> int:
    """How many outbound attempts this process has blocked so far."""
    with _lock:
        return _blocked_total


def status() -> dict[str, Any]:
    """Health-output friendly snapshot. No secrets."""
    with _lock:
        by = dict(_blocked_by_purpose)
        total = _blocked_total
    return {"policy": effective_policy(), "allowHosts": sorted(allowed_hosts()),
            "blockedAttempts": total, "blockedByPurpose": by}


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a new connection: judge its target too."""

    def __init__(self, purpose: str) -> None:
        self._purpose = purpose

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        check_outbound(newurl, purpose=f"{self._purpose}:redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_urlopen(req: urllib.request.Request, *, timeout: float,
                    purpose: str = "central"):  # type: ignore[no-untyped-def]
    """``urllib.request.urlopen`` with the policy applied to the request AND to
    every redirect. Under ``allow-central`` it is exactly ``urlopen`` (upstream
    behaviour). Otherwise environment proxies are ignored so a private target
    can not be tunnelled through an external proxy."""
    if effective_policy() == ALLOW_CENTRAL:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    check_outbound(req.full_url, purpose=purpose)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         _GuardRedirect(purpose))
    return opener.open(req, timeout=timeout)


__all__ = [
    "LOCAL_ONLY", "ALLOW_CENTRAL", "POLICIES", "DEFAULT_POLICY", "ENV_POLICY",
    "ENV_ALLOW_HOSTS", "OutboundBlocked", "configure", "reset", "effective_policy",
    "allowed_hosts", "is_allowed", "check_outbound", "is_local_only",
    "blocked_attempts", "status", "guarded_urlopen",
]
