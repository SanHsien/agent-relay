"""Envelope signatures with OpenSSH (`ssh-keygen -Y sign/verify`), SPEC section 13.

The store is a git branch: anyone who can push to it can write any `from`. A
signature over the envelope itself ties the message to a key, and the receiver
decides out of band (its own allowed_signers file, never the store) which key
speaks for which mailbox. OpenSSH ships with Windows 11 and every CI image, so
there is nothing to install.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from . import procs

NAMESPACE = "agent-relay"
SIG_FIELD = "sig"
TIMEOUT = 30


class SigningError(RuntimeError):
    pass


def canonical(env: dict) -> bytes:
    """The bytes a signature covers: every field except the signature, keys sorted, no whitespace."""
    data = {k: v for k, v in env.items() if k != SIG_FIELD}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


WINDOWS_OPENSSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh-keygen.exe"


def _ssh_keygen() -> str:
    """Windows' own OpenSSH first, whatever PATH says.

    Git for Windows ships another ssh-keygen; a key it creates gets ACLs that Windows
    OpenSSH rejects ("private key files are NOT accessible by others"). A scheduled
    watch and a Git Bash prompt see different PATHs, so a key made in one could not
    sign in the other. Found live: a worker's replies went out unsigned.
    """
    if sys.platform == "win32" and WINDOWS_OPENSSH.is_file():
        return str(WINDOWS_OPENSSH)
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise SigningError("ssh-keygen not found (install the OpenSSH client)")
    return exe


def restrict_key(key: Path) -> None:
    """Windows: only the current user may read the private key (OpenSSH refuses it otherwise)."""
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    for args in (["/inheritance:r"], ["/grant:r", f"{user}:F"]):
        procs.run(["icacls", str(key), *args], capture_output=True, check=False, timeout=TIMEOUT)


def _run(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return procs.run([_ssh_keygen(), *args], input=stdin, capture_output=True, timeout=TIMEOUT,
                              stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise SigningError(f"ssh-keygen timed out: {' '.join(args[:2])}") from exc


def default_key(home: Path, agent: str) -> Path:
    return Path(home) / "keys" / f"{agent}_ed25519"


def keygen(key: Path, agent: str) -> str:
    """Create an ed25519 key with no passphrase (relay signs unattended). Returns the public key line."""
    key = Path(key)
    if key.exists():
        raise SigningError(f"{key} already exists; not overwriting a key")
    key.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["-q", "-t", "ed25519", "-N", "", "-C", f"{NAMESPACE}:{agent}@{socket.gethostname()}",
                 "-f", str(key)])
    if proc.returncode != 0:
        raise SigningError(f"ssh-keygen failed: {proc.stderr.decode(errors='replace').strip()}")
    restrict_key(key)
    return Path(str(key) + ".pub").read_text(encoding="utf-8").strip()


def sign(env: dict, key: Path) -> dict:
    """Return a copy of env with a `sig` field (an armored SSH signature)."""
    with tempfile.TemporaryDirectory(prefix="agent-relay-sign-") as tmp:
        data = Path(tmp) / "envelope.json"
        data.write_bytes(canonical(env))
        proc = _run(["-Y", "sign", "-q", "-f", str(key), "-n", NAMESPACE, str(data)])
        sig = Path(str(data) + ".sig")
        if proc.returncode != 0 or not sig.is_file():
            raise SigningError(f"signing failed: {proc.stderr.decode(errors='replace').strip()}")
        return {**env, SIG_FIELD: sig.read_text(encoding="ascii")}


def verify(env: dict, allowed_signers: Path) -> str | None:
    """None when env carries a valid signature by a key allowed for env['from']; else the reason."""
    sig_text = env.get(SIG_FIELD)
    if not isinstance(sig_text, str) or "BEGIN SSH SIGNATURE" not in sig_text:
        return "unsigned"
    if not Path(allowed_signers).is_file():
        return f"no allowed_signers file at {allowed_signers}"
    with tempfile.TemporaryDirectory(prefix="agent-relay-verify-") as tmp:
        sig = Path(tmp) / "envelope.sig"
        sig.write_text(sig_text, encoding="ascii", errors="replace")
        proc = _run(["-Y", "verify", "-f", str(allowed_signers), "-I", str(env["from"]), "-n", NAMESPACE,
                     "-s", str(sig)], stdin=canonical(env))
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout).decode(errors="replace").strip().splitlines()
    return "bad signature" + (f": {detail[-1]}" if detail else "")


def trust(allowed_signers: Path, agent: str, public_key: str) -> None:
    """Allow public_key to sign as mailbox `agent` (agent-relay namespace only)."""
    from .envelope import safe_name  # the principal field also takes wildcards and lists: "*" would trust a key for every mailbox

    if not safe_name(agent):
        raise SigningError(f"not a mailbox name: {agent!r}")
    parts = public_key.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise SigningError("not an OpenSSH public key line")
    line = f'{agent} namespaces="{NAMESPACE}" {parts[0]} {parts[1]}'
    path = Path(allowed_signers)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if line not in existing:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")


def settings(cfg: dict, home: Path) -> tuple[Path | None, Path]:
    """(this machine's signing key if any, its allowed_signers path)."""
    conf = cfg.get("signing") or {}
    key = Path(os.path.expanduser(conf["key"])) if conf.get("key") else default_key(home, cfg.get("agent", ""))
    allowed = Path(os.path.expanduser(conf["allowed_signers"])) if conf.get("allowed_signers") \
        else Path(home) / "allowed_signers"
    return (key if key.is_file() else None), allowed
