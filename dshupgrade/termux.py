"""The Termux correction: the step a core upgrade is missing on Android.

``dsh-upgrade-tools`` is deliberately platform-neutral, and its "Core upgrade"
step is a plain ``npm i -g @deepseek-ai/dsh@<target>``. That command restores the
**pristine upstream tree** — which on Termux does not run. Android's sepolicy
denies ``link(2)`` in app-private storage, Bionic has no ``flock(2)``, ``sharp``
ships no android-arm64 binary, and several ``process.platform === "linux"``
checks never match ``"android"``. A freshly installed core therefore loses the
``write``/``edit`` file tools, session persistence, attachments, the terminal
shell and image handling, with no error that points at the cause.

Those corrections live in a separate layer — the ``deepseek-harness-termux``
fork — whose anchor-based patcher is re-applied after **every** core install.
This module is the seam that keeps the layer and the core upgrade from being
two unrelated operations:

* it detects Termux (:func:`active`);
* it finds the layer, and clones the fork when it is not on disk
  (:func:`layer`, :func:`ensure_layer`);
* it reads the one dsh version the layer actually validates
  (:func:`validated_version`), so the automatic upgrade target is a version that
  can be patched rather than merely the newest one published;
* it re-applies the layer (:func:`realign`) and probes the native addons
  (:func:`natives_ok`), rebuilding them when the core upgrade replaced them.

The layer is a fork of a community port and is treated as an external tool: it is
run, never edited. Everything here degrades to a printed instruction when the
layer cannot be obtained, so a missing clone makes the step manual — not silent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths
from .semver import compare_version

#: Where the layer comes from when it is not already on disk.
LAYER_REPO = "https://github.com/ThinkForge-core/deepseek-harness-termux.git"

#: The canonical patcher, the re-apply entry point, and the full installer. The
#: first two must both exist for a directory to count as the layer; the patcher is
#: what ``install.sh`` and ``fix-dsh-runtime.sh`` both delegate to, which is why
#: checking it is enough to know the anchor-based fix set is present.
PATCHER_RELATIVE = "scripts/apply-termux-fixes.mjs"
REALIGN_RELATIVE = "fix-dsh-runtime.sh"
INSTALL_RELATIVE = "install.sh"

#: The dsh version the layer's patcher is validated against. It is a constant in
#: ``install.sh`` on purpose: the patcher matches upstream by exact anchors, so a
#: version it has not been validated against fails loudly rather than half-patching.
VALIDATED_RE = re.compile(r'^\s*readonly\s+VALIDATED_DSH_VERSION="([^"]+)"', re.MULTILINE)

#: Probe run from the installed core directory: the three natives a pristine
#: reinstall can lose. ``sharp`` is loaded through its own official WASM fallback,
#: so a failure here is a real one and not a missing platform binary.
#:
#: ``koffi.load(...)`` is NOT redundant. koffi defers loading its addon until the
#: first real call, so a bare ``require('koffi')`` succeeds even when
#: ``koffi.node`` is gone — measured on 0.1.5-rc.2, which is exactly the failure
#: this probe exists to catch (a version bump replaces the compiled binary). The
#: explicit load forces the addon open; do not simplify it back to a bare require.
_NATIVE_PROBE = ("require('node-pty');const k=require('koffi');k.load('libc.so');"
                 "const s=require('sharp');console.log(s.versions.sharp)")


def is_termux() -> bool:
    """Whether this process is running on Termux/Android.

    ``sys.platform`` is the primary signal (CPython reports ``"android"`` there),
    with the two environment markers Termux always sets as a fallback — a Python
    built without Android support still reports ``"linux"``.
    """
    if sys.platform == "android":
        return True
    if os.environ.get("TERMUX_VERSION", "").strip():
        return True
    return "com.termux" in os.environ.get("PREFIX", "")


def active(args=None) -> bool:
    """Whether the Termux correction applies to this run.

    ``--termux`` forces it on, ``--no-termux`` forces it off, and the default
    (``None``) is auto-detection. The explicit off switch matters because the layer
    edits the installed core: a run against a non-Termux core, or a deliberate
    pristine install, must be able to say so.
    """
    setting = getattr(args, "termux", None) if args is not None else None
    if setting is False:
        return False
    if setting is True:
        return True
    return is_termux()


def clone_root() -> Path:
    """Where the tool puts the layer when it has to fetch it."""
    return paths.dsh_home() / "termux-layer"


def looks_like_layer(directory) -> bool:
    """Whether ``directory`` is a checkout of the Termux layer."""
    if not directory:
        return False
    base = Path(directory)
    return (base / PATCHER_RELATIVE).is_file() and (base / REALIGN_RELATIVE).is_file()


def _candidates() -> list[Path]:
    """Where a layer is looked for when none was named, most specific first.

    ``$DSH_TERMUX_DIR``, then the places a clone of this fork actually ends up on
    the devices it targets (next to the other checkouts, under the home directory,
    in the current directory), and finally the tool's own clone root.
    """
    found: list[Path] = []
    environment = os.environ.get("DSH_TERMUX_DIR", "").strip()
    if environment:
        found.append(Path(environment).expanduser())
    home = Path.home()
    found.extend([
        home / "Gits_to_compile" / "deepseek-harness-termux",
        home / "deepseek-harness-termux",
        Path.cwd() / "deepseek-harness-termux",
        clone_root(),
    ])
    return found


def layer(explicit: str | None = None) -> Path | None:
    """The layer to use, or None when none is on disk.

    A directory named by the user is authoritative: when it is not a layer the
    answer is None even if another checkout happens to exist, because quietly
    patching the core from a different directory than the one that was asked for is
    the kind of surprise an operator cannot see in the output.
    """
    explicit = (explicit or "").strip() or None
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if looks_like_layer(candidate) else None
    for candidate in _candidates():
        if looks_like_layer(candidate):
            return candidate.resolve()
    return None


def ensure_layer(explicit: str | None = None, *, clone: bool = True,
                 log=None) -> Path | None:
    """Find the layer, cloning the fork when it is not on disk.

    A layer this tool cloned itself is what makes the correction automatic: without
    it every Termux upgrade would depend on the operator having remembered to fetch
    the fork. A directory the user named is never replaced by a clone — naming one
    that is not a layer is an error to report, not an invitation to guess. When the
    clone cannot run (offline, no ``git``, no network) the result is None and the
    caller prints the manual step instead.
    """
    explicit = (explicit or "").strip() or None
    found = layer(explicit)
    if found is not None:
        return found
    if explicit or not clone:
        return None

    target = clone_root()
    say = log or (lambda _text: None)
    if not shutil.which("git"):
        say("git is not available — cannot fetch the Termux layer")
        return None
    if target.exists():
        say(f"{target} exists but is not a Termux layer — not touching it")
        return None
    say(f"fetching the Termux layer: {LAYER_REPO}")
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "clone", "--depth", "1", LAYER_REPO, str(target)],
        check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        say(f"clone failed: {detail[-1] if detail else completed.returncode}")
        return None
    return layer()


def validated_version(directory) -> str | None:
    """The dsh version the layer's patcher is validated against, if it says so."""
    if not directory:
        return None
    script = Path(directory) / INSTALL_RELATIVE
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = VALIDATED_RE.search(text)
    return match.group(1) if match else None


def target_for(args, newest: str | None) -> tuple[str | None, str | None]:
    """Pick the automatic upgrade target with the layer's validation taken into account.

    Returns ``(version, note)``. The note is set only when the newest published
    version is **not** the one the layer validates, which is the case worth telling
    the reader about: upgrading there would leave a core the anchor-based patcher
    refuses. The cap never moves backwards — if the layer validates something the
    installed core has already passed, the newest version stands and the note says
    the layer has not caught up, because a downgrade is not a remedy the pipeline
    can perform.
    """
    if newest is None or not active(args):
        return newest, None
    found = layer(getattr(args, "termux_dir", None))
    validated = validated_version(found)
    if not found or not validated or validated == newest:
        return newest, None

    installed = paths.core_version()
    if installed is not None:
        if compare_version(validated, installed) <= 0:
            return newest, (f"the Termux layer validates {validated}, which the installed core "
                            f"has reached already — {newest} has NOT been validated, so its "
                            "patches may refuse it: update the layer first")
    return validated, (f"the Termux layer validates {validated}, so that is the target "
                       f"rather than the newest {newest}")


def commands(directory, version: str | None = None, *, rebuild: bool = False) -> list[str]:
    """The steps that must follow the ``npm i -g`` core upgrade, in order.

    The order is the whole point: ``npm i -g`` restores pristine files, the layer
    puts the Android corrections back, and only then may the plugins be installed —
    a plugin judged against an unpatched core is judged against a core that cannot
    write a file on this platform. ``rebuild`` adds the full installer, which is what
    recompiles ``node-pty``/``koffi`` when a version bump replaced their binaries.

    Empty when there is no layer: the caller then prints the bare ``attach`` step, as
    it did before this module existed.

    The script is named by its absolute path because the reader is not standing in
    the layer — both entry points resolve their own directory from ``$0``, so an
    absolute path is runnable from anywhere, while a bare ``bash fix-dsh-runtime.sh``
    in a printed hint is not.
    """
    if not directory:
        return []
    base = Path(directory)
    sequence = [f"bash {base / REALIGN_RELATIVE}"]
    if rebuild:
        installer = f"bash {base / INSTALL_RELATIVE}"
        if version:
            installer += f" {version}"
        sequence.append(installer + "   # rebuilds node-pty/koffi")
    return sequence


def realign(directory, *, log=None) -> int:
    """Re-apply the layer's patches to the installed core; returns the exit code.

    Idempotent and safe on a pristine tree — that is the property the whole design
    rests on (``install.sh`` and ``fix-dsh-runtime.sh`` share one patcher), so this
    can run unconditionally after an upgrade instead of first asking whether the core
    looks patched.
    """
    say = log or (lambda _text: None)
    script = Path(directory) / REALIGN_RELATIVE
    if not script.is_file():
        say(f"the Termux layer has no {REALIGN_RELATIVE}")
        return 1
    completed = subprocess.run(["bash", str(script)], cwd=str(directory), check=False)
    return completed.returncode


def rebuild(directory, version: str | None = None, *, log=None) -> int:
    """Run the layer's full installer, which recompiles the native addons."""
    say = log or (lambda _text: None)
    script = Path(directory) / INSTALL_RELATIVE
    if not script.is_file():
        say(f"the Termux layer has no {INSTALL_RELATIVE}")
        return 1
    command = ["bash", str(script)]
    if version:
        command.append(version)
    completed = subprocess.run(command, cwd=str(directory), check=False)
    return completed.returncode


def _failure_line(text: str, fallback: str) -> str:
    """The line of a captured stderr that names the failure.

    A Node stack trace leads with its own machinery (``node:internal/...``, a
    ``throw err;`` marker) and buries the sentence that matters below it, so the
    first line that actually names an error wins; without one the last non-empty
    line is the most specific thing available.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("Error", "TypeError", "ReferenceError", "SyntaxError")):
            return line
    return lines[-1] if lines else fallback


def natives_ok(install_dir=None) -> tuple[bool, str]:
    """Whether the three native addons load from the installed core.

    Run from the core directory so the resolution is the one dsh itself performs.
    A missing ``node`` is reported as a failure with its own reason rather than
    raising: the caller is a report, and "cannot ask" must not read as "broken".
    """
    directory = install_dir or paths.core_install_dir()
    if directory is None:
        return False, "the installed core was not found"
    node = shutil.which("node")
    if not node:
        return False, "node is not on PATH"
    completed = subprocess.run([node, "-e", _NATIVE_PROBE], cwd=str(directory),
                               check=False, capture_output=True, text=True)
    if completed.returncode == 0:
        return True, (completed.stdout or "").strip()
    return False, _failure_line(completed.stderr or completed.stdout,
                                f"exit {completed.returncode}")


def summary(args=None) -> dict:
    """What the tool knows about the layer, for a report to show.

    ``active`` and ``layer`` follow the run's own switch, so ``--no-termux`` reads as
    the correction being off rather than as a layer that is present but ignored.
    ``platform`` stays a fact about the machine either way.
    """
    on = active(args)
    explicit = getattr(args, "termux_dir", None) if args is not None else None
    found = layer(explicit) if on else None
    return {
        "platform": "termux" if is_termux() else "other",
        "active": on,
        "layer": str(found) if found else None,
        "validated": validated_version(found),
    }
