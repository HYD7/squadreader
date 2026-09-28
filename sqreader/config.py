"""Machine-specific runtime configuration.

The reader is driven by CLI flags first; anything not passed falls back to this
config, and anything not in the config falls back to a built-in default. So a
stranger on a standard box needs NO config at all — the Squad server is found by
its process name and output dirs default next to the repo. Only non-standard
setups (an install path outside ``/home/<user>/serverfiles``, a custom server
id) need a ``sqreader.config.json`` — copy ``sqreader.config.example.json``.

Resolution order for every key:  CLI flag  >  sqreader.config.json  >  DEFAULTS.
The config file is located via ``$SQREADER_CONFIG``, else ``./sqreader.config.json``.
"""
from __future__ import annotations

import json
import os
import subprocess  # noqa: F401  (legacy attribute; tests patch config.subprocess)
from pathlib import Path
from typing import Any

from . import discovery

DEFAULTS: dict[str, Any] = {
    # The Squad dedicated-server binary name. This is a Squad constant (the same
    # on every install), exposed only so an exotic rename can be handled without
    # editing source.
    "squad_process_name": "SquadGameServer",
    # DEPRECATED, unused since process discovery moved to sqreader/discovery.py
    # (kept so existing config files still load).
    "squad_binary_pattern": r"/home/.*/serverfiles/.*SquadGameServer",
    # The game port of the instance THIS agent reads (Squad's ``-Port=``), for
    # boxes running several servers. Unset means "the only one there is", which
    # is resolved by name and is a coin toss the moment there are two. Set it
    # and the choice stops depending on which server happened to start first.
    "squad_port": None,
    # Squad's ``-QueryPort=`` of the instance THIS reader reads. Optional second
    # criterion (or the only one, if that is the number you know). The reader
    # reports which of Port / QueryPort matched. See sqreader/discovery.py.
    "squad_query_port": None,
    # Container id (64 hex, or a prefix of at least 12) the game must run in.
    # Optional extra safety when several servers share a host. A re-created
    # container gets a NEW id: after re-creating it, update this value; until
    # then the reader refuses (visibly) to attach to the new process.
    "expected_container_id": None,
    # Where the log lives INSIDE the container; the reader reads it through
    # /proc/<pid>/root. None = /home/container/SquadGame/Saved/Logs/SquadGame.log
    "log_path_in_container": None,
    # Explicit log file path on the reader's own filesystem. Wins over
    # everything; None = derive it from the discovered process.
    "log_path": None,
    # Fallback for non-Pterodactyl installs ONLY (used when the process is not
    # known to be in a container and its own log cannot be found). More than one
    # match is an error, never "the newest".
    "squad_log_glob": "/home/*/serverfiles/SquadGame/Saved/Logs/SquadGame.log",
    # Default value of the snapshot ``server`` field and the stats-DB partition
    # key. Override per deployment (e.g. "eu-1") when you run more than one.
    "server_id": "squad",
    # Unlicensed (custom) Squad servers never get a MatchID from OWI's backend,
    # which silently disables BOTH the recorder and the stats store. When true,
    # the agent derives a stable id for those servers instead. Inert on licensed
    # servers, which always supply their own. See sqreader/synth_match.py.
    "synthetic_match_ids": True,
    # --- optional central push (OFF by default; opt-in via `sqreader enroll`) ---
    # If set + the box is enrolled (see agent_creds/.env.agent), finished-match
    # stats + .sqrx replays are pushed to this central. NON-SECRET keys only —
    # the enrollment secret lives in the 0600 .env.agent, never here.
    "central_url": None,
    # Master switch. Even when enrolled, push is skipped unless this is true (or
    # `serve --push` is passed). Keeps a fresh install fully local.
    "push_enabled": False,
    # Whether to also upload the match's .sqrx replay (not just the stats).
    "push_replay": True,
    # Where the pending-push queue lives; default is next to the stats DB.
    "push_backlog_dir": None,
    # Remote-update rollout channel this agent follows. "stable" gets promoted
    # packs/releases; "beta" opts into canary ones first. Reported on each fleet
    # check-in and used by central to decide what (if anything) to serve us.
    "update_channel": "stable",
    # Auto-apply signed offset packs when a Squad patch drifts the reader
    # (self-heal). Verified (Ed25519) → applied → re-checked with doctor → rolled
    # back if it doesn't clear the drift. Only ever active on an enrolled,
    # push-enabled box. Set false to require manual offset updates.
    "offset_autoheal": True,
    # Fetch + verify + apply signed agent code releases (self-update).
    #
    # ON by default, but that default can only ever fire on a box that is
    # ENROLLED and pushing: the check lives inside the fleet check-in, which
    # only runs when a central is configured and reachable. A self-hosted
    # install has no central to fetch from, so the flag is inert there — and
    # remote update is precisely what an operator gets by using squadreader.com
    # instead of running their own. Set false to pin the binary and upgrade by
    # hand.
    #
    # The running reader still never overwrites itself. It stages the verified
    # artifact, waits for a moment with no match in progress, and exits;
    # systemd's ExecStartPre runs `sqreader apply-staged-update`, which does the
    # swap between the two processes and rolls back if the new binary will not
    # boot. See sqreader/updater.py.
    "update_enabled": True,

    # Path to plugins_config.json, for deployments whose start command is not
    # ours to edit - a container entrypoint baked into an image, a unit file
    # someone else manages. `--plugins-config` still wins when it is given.
    # Without either, no plugin is constructed at all.
    "plugins_config": None,

    # Where to announce plugin alerts. A Discord webhook URL, or None to keep
    # them in the database only. This is a CREDENTIAL — anyone holding it can
    # post into that channel — so it lives in the config file rather than in a
    # command line other users on the box can read out of `ps`.
    "alert_webhook": None,
    # Alerts below this confidence are stored but not announced. Detectors are
    # tuned to be noisy on purpose so the evidence is kept; a channel humans
    # actually read wants a higher bar than a table does.
    "alert_min_confidence": 0.0,
    # Base URL used to build "watch the replay" links, e.g.
    # "https://squadreader.com". Without it an alert still arrives, just
    # without a way to go and look at it.
    "alert_replay_base": None,

    # Export API v1 (sqreader/export_api.py): a token-protected, read-only pull
    # interface for finished matches and their replays. Path to a file holding
    # the bearer token (>= 32 characters; keep it mode 0600). None / missing /
    # empty file = the API is OFF and every /export/v1/* path is a plain 404.
    # `serve --export-token-file` wins over this. The token itself never goes
    # in this config file.
    "export_token_file": None,
}

_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    cfg = dict(DEFAULTS)
    env = os.environ.get("SQREADER_CONFIG")
    path = Path(env) if env else Path.cwd() / "sqreader.config.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Skip `_comment` annotation keys used in the example file.
                cfg.update({k: v for k, v in data.items()
                            if not k.startswith("_")})
        except (OSError, ValueError):
            pass
    _cache = cfg
    return cfg


def get(key: str) -> Any:
    """Config value for ``key`` (config file overrides the built-in default)."""
    return _load().get(key, DEFAULTS.get(key))


def _has_game_port(args: list[str], port: int) -> bool:
    """Whether a command line names ``port`` as Squad's GAME port (``Port=``).

    Legacy helper; the real matching is ``discovery.parse_ports``. ``QueryPort=``
    and ``BeaconPort=`` carry port numbers too, and the number must match whole:
    on a box where one server's game port is another's query port, a substring
    test picks the wrong game.
    """
    return port in discovery.parse_ports(args)[discovery.KIND_GAME]


def _is_game_binary(pid: str, proc: str) -> bool:
    """Whether ``pid`` IS the game server rather than something that starts it.

    Legacy helper, kept for callers that only have a PID. Not a `comm` check:
    the kernel truncates comm to 15 bytes, so LinuxGSM's ``SquadGameServer.sh``
    launcher reports exactly ``SquadGameServer``, the same as the binary. The exe
    link is what separates them. Unreadable means no.
    """
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe")) == proc
    except OSError:
        return False


def discovery_criteria(**overrides: Any) -> dict[str, Any]:
    """The discovery criteria the config asks for (overrides win)."""
    crit: dict[str, Any] = {
        "exe_names": _exe_names(),
        "game_port": get("squad_port"),
        "query_port": get("squad_query_port"),
        "expected_container_id": get("expected_container_id"),
        "log_path_in_container": get("log_path_in_container"),
    }
    crit.update(overrides)
    return crit


def _exe_names() -> tuple[str, ...]:
    name = get("squad_process_name")
    if not name:
        return tuple(discovery.DEFAULT_EXE_NAMES)
    return (str(name),) + tuple(n for n in discovery.DEFAULT_EXE_NAMES
                                if n != name)


def resolve_game_process(pid: int | None = None) -> discovery.GameProcess:
    """The ONE game process this reader targets, or a ``DiscoveryError``.

    With ``pid`` (``--pid``): that process is VERIFIED against the same criteria
    (binary name, ports, container) instead of being trusted. Do not pin a PID
    in a unit or config: PIDs are transient. Without it: discovery by the
    configured criteria; zero matches and several matches are both errors.
    """
    crit = discovery_criteria()
    if pid:
        return discovery.verify_game_process(int(pid), **crit)
    return discovery.discover_game_process(**crit)


def _pid_by_game_port(proc: str, port: int) -> int | None:
    """PID of the single ``proc`` instance with ``Port=<port>``, else None."""
    try:
        return discovery.discover_game_process(
            **discovery_criteria(exe_names=(proc,), game_port=port)).pid
    except discovery.GameProcessNotFound:
        return None


def find_squad_server_pid() -> int:
    """Resolve the Squad dedicated-server PID (legacy int-returning API).

    Prefer ``resolve_game_process``: it returns the identity too
    (``pid`` + ``start_time_ticks``), which is what protects against PID reuse.

    ``squad_port`` decides it outright when set. Missing is an error rather than
    a fallback: reading a server the operator did not ask for is worse than
    reading none, because every match it publishes lands under the wrong
    server's name. Without a port criterion, more than one running server is an
    ERROR too; the first one is never picked.
    """
    proc = get("squad_process_name")
    port = get("squad_port")
    try:
        if (port and get("squad_query_port") is None
                and get("expected_container_id") is None):
            pid = _pid_by_game_port(proc, int(port))
            if pid is not None:
                return pid
            raise SystemExit(f"no {proc} running with -Port={port} "
                             f"(squad_port is set, so no other instance will do)")
        return resolve_game_process().pid
    except discovery.DiscoveryError as e:
        raise SystemExit(str(e)) from e


__all__ = ["DEFAULTS", "get", "find_squad_server_pid",
           "resolve_game_process", "discovery_criteria"]
