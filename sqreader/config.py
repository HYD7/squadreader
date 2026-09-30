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
    # Outbound network policy (sqreader/netpolicy.py). "local-only" (default):
    # only loopback / private ranges / hosts listed verbatim in net_allow_hosts
    # may be contacted; no central platform, no update server, no chat webhook
    # on the public internet. "allow-central" restores the upstream behaviour
    # (enroll / push / check-in / offset packs / self-update) and must be chosen
    # explicitly. The SQREADER_NET_POLICY / SQREADER_NET_ALLOW_HOSTS environment
    # variables win over these keys.
    "net_policy": "local-only",
    "net_allow_hosts": [],
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
    "offset_autoheal": False,
    # Fetch + verify + apply signed agent code releases (self-update).
    #
    # OFF by default in this fork (no runtime dependency on an external
    # service): the check lives inside the fleet check-in, which only runs on an
    # ENROLLED box that is pushing to a central, and the network policy keeps
    # that shut in local mode anyway. Turn it on only together with
    # net_policy=allow-central. Upgrade by hand otherwise.
    #
    # The running reader still never overwrites itself. It stages the verified
    # artifact, waits for a moment with no match in progress, and exits;
    # systemd's ExecStartPre runs `sqreader apply-staged-update`, which does the
    # swap between the two processes and rolls back if the new binary will not
    # boot. See sqreader/updater.py.
    "update_enabled": False,

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
    # Directory of export_state.json / export_audit.log / retention_state.json
    # (the recording switch, the acks, the audit trail). None = the parent of the
    # recordings directory.
    "export_state_dir": None,

    # --- recording retention + disk quota (sqreader retention, and the recorder's
    # low-disk guard). 0 switches a limit off. Only FINALIZED recordings that the
    # export consumer has ACKNOWLEDGED (POST /export/v1/ack) are removed by the
    # age / size rules; an un-acked one is removed only in a disk EMERGENCY (free
    # space below retention_min_free_gb) and every such removal is logged and
    # audited. The active recording and any recording that is not finalized are
    # never touched (unverified ones only on request, see below).
    "retention_max_age_days": 90,
    "retention_max_total_gb": 150,
    # Free-space floor of the recordings filesystem. Below it: retention removes
    # the oldest recordings (acked first), and if that frees nothing the recorder
    # opens NO new recording (health: recordingEnabledReason=disk_low). Stats
    # collection and the game server are unaffected.
    "retention_min_free_gb": 5,
    # Never delete a file modified in the last N minutes / the newest N finalized
    # recordings on the free-space rule.
    "retention_in_progress_min": 60,
    "retention_min_keep": 3,
    # null = require an ack exactly when the Export API is on (export_token_file
    # set); true / false force it.
    "retention_require_ack": None,
    # Unverified / legacy recordings (never proven finalized) are removed only when
    # this is true AND they are older than retention_unverified_max_age_days.
    "retention_include_unverified": False,
    "retention_unverified_max_age_days": 180,

    # Zero Zone deployment guard: when true, `serve` REFUSES to start unless at
    # least one target criterion is configured (squad_port, squad_query_port or
    # expected_container_id), exit code 2. Without a criterion the reader picks
    # "the only SquadGameServer there is", which on a host with a second (not yet
    # migrated) server is a coin toss. null (default) = require it exactly when
    # the Export API is configured (export_token_file / --export-token-file
    # set), i.e. in every Zero Zone deployment and nowhere else; true / false
    # force it. Stock upstream behaviour (no criteria needed) is unchanged.
    "require_target": None,
}

_cache: dict[str, Any] | None = None


class ConfigError(Exception):
    """The config file exists but cannot be used (unreadable / not a JSON
    object). Operator-facing message; the CLI turns it into exit code 2."""


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    cfg = dict(DEFAULTS)
    env = os.environ.get("SQREADER_CONFIG")
    path = Path(env) if env else Path.cwd() / "sqreader.config.json"
    if path.is_file():
        # A file that is THERE but cannot be used must not silently become "all
        # defaults": that would drop squad_port and the reader would attach to
        # whichever game server it finds first. (A file that does not exist at
        # all keeps the zero-config behaviour: stock upstream needs no config,
        # and Zero Zone's `serve` refuses to run without a target criterion.)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:      # includes UnicodeDecodeError
            raise ConfigError(
                f"yapılandırma dosyası okunamadı / ayrıştırılamadı: {path} "
                f"({type(e).__name__}: {e}). Varsayılanlara DÖNÜLMEZ; dosyayı "
                f"düzeltin. / config file unreadable or not valid JSON; "
                f"refusing to fall back to defaults.") from e
        if not isinstance(data, dict):
            raise ConfigError(
                f"yapılandırma dosyası bir JSON nesnesi olmalı: {path}. / "
                f"config file must be a JSON object.")
        # Skip `_comment` annotation keys used in the example file.
        cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
    _cache = cfg
    return cfg


def get(key: str) -> Any:
    """Config value for ``key`` (config file overrides the built-in default)."""
    return _load().get(key, DEFAULTS.get(key))


def require_target_enabled(export_token_file: Any = None) -> bool:
    """Whether `serve` must refuse to start without a target criterion.

    `require_target` true / false wins; null means "exactly when the Export API
    is configured" (the CLI flag ``export_token_file`` or the config key).
    """
    forced = get("require_target")
    if forced is not None:
        return bool(forced)
    return bool(export_token_file or get("export_token_file"))


def require_target_criteria() -> None:
    """Raise ``discovery.DiscoveryConfigError`` unless the config names at least
    one target criterion (game port, query port or container id)."""
    crit = discovery_criteria()
    discovery.require_target_criteria(
        game_port=crit["game_port"], query_port=crit["query_port"],
        expected_container_id=crit["expected_container_id"])


def check_instance(instance: str) -> None:
    """Raise ``discovery.DiscoveryConfigError`` unless the systemd instance name
    is a port the config names: `squad_port` OR `squad_query_port`
    (`sqreader@7787` <-> squad_port 7787, or squad_port null + squad_query_port
    7787)."""
    discovery.check_instance_port(instance, get("squad_port"),
                                  get("squad_query_port"))


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
