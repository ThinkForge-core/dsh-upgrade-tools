"""Wire contract: does the installed core still serve the calls a plugin makes?

The other two questions a plugin can fail are answered elsewhere. The declarations
say "may this plugin run on that core" (:mod:`dshupgrade.compat`); the probe says
"does its entry import and something register" (:mod:`dshupgrade.verify`);
:mod:`dshupgrade.effects` says "is the component its client half draws into
mounted". None of them looks at the calls the plugin makes at run time, and that is
the failure this module covers: a client half that POSTs an RPC path the core has
since renamed. Its code is valid, its import is clean, its `apply()` runs — and the
call answers 404, so the feature silently does nothing.

The ordinary shape of it: a client half resolves a row by POSTing
``/api/session.list`` with ``method: "session.list"``. The core's shared ``/api``
channel is claimed by the Typert gateway, whose endpoints are
``<namespace>/<method>``, so the call answers 404 and the menu item is never
appended. Nothing but the browser console shows it.

Why this is checkable without a running host: the installed core's generated TYPERT
faces declare every endpoint it serves as ``namespace: '<ns>', method: '<method>'``,
so the served set is read from the core's own bytes. The live probe cannot do this
job: ``/api`` answers 401 to an unauthenticated request before it routes, so every
path — served or not — looks alike from outside.

Scope, stated honestly:

* Only literal ``/api`` paths are read. A URL assembled from variables is not
  followed, and nothing is reported for it.
* Only files carrying the ``client-request`` envelope marker are treated as RPC
  callers. A bare ``/api/...`` path elsewhere is a plain webServer route, which is
  out of the RPC contract and already covered by the route probe.
* ``/api-ext/...`` is excluded by construction: those are the plugin's own
  extension routes, registered by its host half, not core RPC endpoints.
* The endpoint set is read from packages that publish a ``typert`` export. An
  endpoint served by a mechanism that publishes no such face is invisible here.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from .effects import client_half
from .paths import core_install_dir, host_modules_dir, read_json

#: The call is served by the installed core.
OK = "ok"
#: The path (or the envelope's method) is not an endpoint of the installed core.
DEAD = "dead"
#: The path and the envelope's own ``method`` disagree; the host rejects those.
MISMATCH = "mismatch"
#: Not classified yet — a call read from a directory with no core to compare with.
UNCHECKED = "unchecked"

#: One endpoint of the installed core, as its generated TYPERT face declares it.
_INVOCATION_RE = re.compile(
    r"namespace:\s*'([A-Za-z0-9_-]+)',\s*method:\s*'([A-Za-z0-9_-]+)'")

#: A literal HTTP path under the RPC channel. ``/api-ext/...`` does not match.
_PATH_RE = re.compile(r"""["'`](/api(?:/[A-Za-z0-9_.\-]+)+)["'`]""")

#: The envelope's own method. The separator is required, which is what keeps
#: ``method: "POST"`` (the fetch option) out of the results.
_METHOD_RE = re.compile(r"""\bmethod:\s*["']([A-Za-z0-9_-]+[./][A-Za-z0-9_./-]*)["']""")

#: The Connection RPC envelope. A file without it is not an RPC caller.
_ENVELOPE_RE = re.compile(r"""["']client-request["']""")


@dataclass
class CoreEndpoints:
    """The wire endpoints the installed core declares, read from its own bytes."""

    endpoints: frozenset[str] = frozenset()
    files: int = 0
    packages: int = 0
    directory: str | None = None

    def family(self, namespace: str) -> list[str]:
        return sorted(endpoint for endpoint in self.endpoints
                      if endpoint.startswith(f"{namespace}/"))

    def suggestion(self, form: str) -> str:
        """The closest endpoints, for a call that matches none of them."""
        namespace = re.split(r"[/.]", form, maxsplit=1)[0]
        family = self.family(namespace)
        if not family:
            return ""
        close = difflib.get_close_matches(form.replace(".", "/"), family, n=3, cutoff=0.5)
        if not close:
            return f"this core serves {len(family)} endpoint(s) in '{namespace}', none close"
        return "closest: " + ", ".join(close)


@dataclass
class WireCall:
    """One literal RPC path a plugin calls, and whether the core serves it."""

    plugin: str
    path: str
    #: The endpoint the path names, e.g. ``session.list`` or ``session/list``.
    form: str
    #: The envelope's own ``method`` value, when the file names one nearby.
    method: str | None = None
    source: str = ""
    verdict: str = UNCHECKED
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "plugin": self.plugin,
            "path": self.path,
            "form": self.form,
            "method": self.method,
            "source": self.source,
            "verdict": self.verdict,
            "note": self.note,
        }


def _typert_files(package_dir: Path) -> list[Path]:
    """The generated TYPERT faces one core package publishes."""
    manifest = read_json(package_dir / "package.json")
    if not isinstance(manifest, dict):
        return []
    exports = manifest.get("exports")
    if not isinstance(exports, dict):
        return []
    files: list[Path] = []
    for key, value in exports.items():
        if "typert" not in key:
            continue
        relative = value.get("default") if isinstance(value, dict) else value
        if not isinstance(relative, str):
            continue
        candidate = package_dir / relative.lstrip("./")
        if candidate.is_file():
            files.append(candidate)
    return files


def core_endpoints(directory: Path | None = None) -> CoreEndpoints:
    """Read every wire endpoint the installed core declares.

    One pass over the ``typert`` faces of the packages beside the core's own
    manifest — the same bytes the gateway builds its claim set from.
    """
    install = Path(directory) if directory is not None else core_install_dir()
    if install is None:
        return CoreEndpoints()
    modules = host_modules_dir(install)
    if modules is None:
        return CoreEndpoints(directory=str(install))
    packages = modules / "@deepseek-ai"
    found: set[str] = set()
    seen_files = 0
    seen_packages = 0
    for package_dir in sorted(packages.iterdir()):
        files = _typert_files(package_dir)
        if not files:
            continue
        seen_packages += 1
        for path in files:
            seen_files += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found.update(f"{namespace}/{method}"
                         for namespace, method in _INVOCATION_RE.findall(text))
    return CoreEndpoints(endpoints=frozenset(found), files=seen_files,
                         packages=seen_packages, directory=str(install))


def _entry_files(directory: Path) -> list[Path]:
    """The files worth scanning: the client half and the host entry."""
    manifest = read_json(directory / "package.json")
    files: list[Path] = []
    half = client_half(directory, manifest if isinstance(manifest, dict) else None)
    if half.entry is not None:
        files.append(half.entry)
    if isinstance(manifest, dict):
        exports = manifest.get("exports")
        relative = exports.get(".") if isinstance(exports, dict) else None
        if isinstance(relative, dict):
            relative = relative.get("default")
        if not isinstance(relative, str):
            relative = manifest.get("main")
        if isinstance(relative, str):
            candidate = directory / relative.lstrip("./")
            if candidate.is_file():
                files.append(candidate)
    unique: list[Path] = []
    for path in files:
        if path not in unique:
            unique.append(path)
    return unique


def calls_in_text(plugin: str, text: str, name: str = "client.js") -> list[WireCall]:
    """Every literal RPC path in one file, paired with the nearest method literal."""
    if not _ENVELOPE_RE.search(text):
        return []
    methods = [(match.start(), match.group(1)) for match in _METHOD_RE.finditer(text)]
    calls: list[WireCall] = []
    for match in _PATH_RE.finditer(text):
        path = match.group(1)
        if path.startswith("/api-ext"):
            continue
        method = None
        if methods:
            # The envelope's method literal is written beside the path it belongs
            # to, and a file may hold several calls: take the nearest one.
            distance, method = min(
                ((abs(offset - match.start()), value) for offset, value in methods),
                key=lambda pair: pair[0])
        calls.append(WireCall(plugin=plugin, path=path, form=path[len("/api/"):],
                              method=method,
                              source=f"{name}:{text.count(chr(10), 0, match.start()) + 1}"))
    return calls


def plugin_calls(plugin_dir: Path, plugin: str) -> list[WireCall]:
    """Read a plugin's own files for literal RPC calls."""
    directory = Path(plugin_dir)
    found: list[WireCall] = []
    for path in _entry_files(directory):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.extend(calls_in_text(plugin, text, path.name))
    return found


def calls_in_source(plugin: str, files: dict[str, str]) -> list[WireCall]:
    """Every literal RPC call in an artifact's already-read files.

    A directory and a tarball are reduced to the same ``name -> text`` mapping by
    :class:`dshupgrade.compat.CodeSource`, so an artifact that is not installed is
    scanned exactly like an installed plugin.
    """
    found: list[WireCall] = []
    for name, text in sorted(files.items()):
        found.extend(calls_in_text(plugin, text, name))
    return found


def check_source(plugin: str, files: dict[str, str],
                 core: CoreEndpoints) -> list[WireCall]:
    """Classify an artifact's calls against a core; returns the broken ones."""
    return check_calls(calls_in_source(plugin, files), core)


def classify(call: WireCall, core: CoreEndpoints) -> WireCall:
    """Fill in one call's verdict against the installed core's endpoints."""
    if not core.endpoints:
        call.verdict = UNCHECKED
        call.note = "no installed core to compare with"
        return call
    if call.form in core.endpoints:
        if call.method is not None and call.method != call.form:
            call.verdict = MISMATCH
            call.note = (f'the endpoint is "{call.form}", but the envelope sends '
                         f'method "{call.method}"')
            return call
        call.verdict = OK
        return call
    call.verdict = DEAD
    slashed = call.form.replace(".", "/")
    if slashed in core.endpoints:
        call.note = (f'the legacy separator: this core serves "{slashed}" '
                     "(the gateway claims <namespace>/<method>)")
        return call
    call.note = core.suggestion(call.form) or "no endpoint in this namespace"
    return call


def check_calls(calls: list[WireCall], core: CoreEndpoints) -> list[WireCall]:
    """Classify every call in place and return the non-``ok`` ones."""
    return [call for call in (classify(call, core) for call in calls)
            if call.verdict != OK]


def scan_profile(profile, core: CoreEndpoints | None = None) -> dict[str, list[WireCall]]:
    """Dead or mismatched RPC calls per installed plugin of the profile.

    Reads files only, so it runs on the ``status`` path as well and needs no host.
    Plugins that make no RPC call at all contribute nothing.
    """
    core = core if core is not None else core_endpoints()
    found: dict[str, list[WireCall]] = {}
    for plugin in profile.plugins:
        if not plugin.installed or not plugin.directory:
            continue
        broken = check_calls(plugin_calls(Path(plugin.directory), plugin.name), core)
        if broken:
            found[plugin.name] = broken
    return found


def total(wire: dict[str, list[WireCall]]) -> int:
    """How many broken calls the scan found, over every plugin."""
    return sum(len(calls) for calls in wire.values())
