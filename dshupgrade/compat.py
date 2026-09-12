"""Compatibility checks between a plugin and a core version.

Six independent checks — they catch different classes of breakage, and each of
them alone gives a false "all good":

1. **Declarations** (peer ranges + ``engines.dsh``) — the way the market counts:
   the rules are an independent Python port of the compatibility engine of the
   marketplace plugin ``dshmarket`` (https://github.com/dsh-market/dsh-market),
   which cannot be imported — it is Node code, and it is detached together with
   every other plugin for the duration of an upgrade. See README, "Attribution".
2. **Removed packages** — hard ``require``/``import`` of names that no longer
   exist in the target version (checked against the target checkout inventory).
3. **Browser module table** — ``require`` in the client bundle of names that are
   in neither the seed words nor the inventory of the target version.
4. **Declaration integrity** — the ``dsh.client`` object and the
   ``exports["./client"]`` bundle it promises. The host composes the boot graph
   from the first and fetches the second, so a declaration the host rejects or a
   bundle that is not in the artifact fails on ANY core, whatever the versions say.
5. **Inline purity** — a client bundle that inlines a package the host loads as
   its own module-table row. The duplicate copy shares no state with the host's,
   so nothing throws: a panel just stays empty (or the bundle installs a second
   factory and boot dies loudly). This mirrors the core's own build-time gate.

Note: ``dsh.client.inject`` is **informational** (the 0.1.5 types say literally
"Informational package-name dependencies, not Cordis service injection"); only a
real ``require`` inside the bundle causes a failure, and only
``dsh.client.external`` lifts the inline-purity rule.

Checks 2-5 work against an abstract :class:`CodeSource`, not just a directory: a
hand-built plugin may live in a tarball (``file:…tgz``) and its code is read
straight from the archive without unpacking it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import semver
from .host import host_peer_names

_REQUIRE_RE = re.compile(
    r"""(?:\brequire\s*\(\s*|\bimport\s*\(\s*|\bfrom\s*)['"]([^'"\n]+)['"]""",
)

# An npm package name (with an optional scope and subpath). Filters out the junk
# that minified code would otherwise produce: a string like
# `from "text with spaces"` or `from "${spec}"` is not a module request.
_SPECIFIER_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:/[A-Za-z0-9._-]+)*$"
)

CODE_SUFFIXES = (".js", ".mjs", ".cjs")

# Directories that are never scanned: somebody else's code in node_modules and
# the service directories of version control systems / caches.
SKIP_PARTS = frozenset({"node_modules", ".git"})

# Extra exclusions for a DIRECTORY SOURCE (a link to the plugin repository
# rather than to a built package): tests and examples are not published and
# would produce false "hard link to a removed package" hits.
REPO_SKIP_PARTS = SKIP_PARTS | frozenset({
    "test", "tests", "__tests__", "spec", "specs", "fixtures", "examples", "docs",
})

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 5000


def _is_module_specifier(specifier: str) -> bool:
    """Does the value look like a module name (and not like a code string literal)?"""
    return _SPECIFIER_RE.match(specifier) is not None


STATUS_COMPATIBLE = "compatible"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_UNKNOWN = "unknown"

# Outcomes that the market treats as a definite incompatibility.
DEFINITE_FAILURES = ("below-min", "above-explicit-max", "exact-pin")


@dataclass
class Declaration:
    """A single declared requirement on the DSH version."""

    kind: str  # peer | engine | engine-nested
    range: str
    package: str | None = None
    source: str = ""
    result: bool | None = None
    direction: str | None = None

    def describe(self) -> str:
        who = self.package or "@deepseek-ai/dsh"
        return f"{who} {self.range}"


@dataclass
class Verdict:
    """Result of the declaration check."""

    status: str
    basis: str
    requirement: str | None = None
    declarations: list[Declaration] = field(default_factory=list)

    @property
    def failing(self) -> list[Declaration]:
        return [d for d in self.declarations if d.result is False]

    def reason(self) -> str:
        """Human readable reason for the verdict (empty when nothing is wrong)."""
        if self.status == STATUS_COMPATIBLE:
            return ""
        if self.status == STATUS_UNKNOWN and self.basis == "undeclared":
            return "no requirement declarations for the DSH version"
        if self.status == STATUS_UNKNOWN and self.basis == "unavailable":
            return "manifest unavailable"
        parts = []
        for item in self.failing:
            parts.append(f"{item.describe()} → {item.direction}")
        return "; ".join(parts) or self.basis


def declarations_for(manifest: dict, host_packages: set[str]) -> list[Declaration]:
    """Collect the DSH version requirements declared by a plugin manifest."""
    declarations: list[Declaration] = []

    # 1. Top level engines.dsh — the ONLY thing the market engine reads.
    engines = manifest.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("dsh"), str):
        declarations.append(Declaration("engine", engines["dsh"], source="engines.dsh"))

    # 2. Nested dsh.engines.dsh — nobody reads this one (neither the core nor the
    #    market), but the requirement is real in the ecosystem: plugins do declare
    #    their minimum core in here.
    dsh_section = manifest.get("dsh")
    if isinstance(dsh_section, dict):
        nested = dsh_section.get("engines")
        if isinstance(nested, dict) and isinstance(nested.get("dsh"), str):
            declarations.append(Declaration("engine-nested", nested["dsh"], source="dsh.engines.dsh"))

    # 3. peerDependencies on host packages — the market policy.
    for name, value in host_peer_names(manifest, host_packages).items():
        declarations.append(Declaration("peer", value, package=name, source="peerDependencies"))

    return declarations


def _alternative_lower_bound(alternative: str):
    """Lowest bound of one range alternative (or None)."""
    lower = None
    for comparator in alternative.strip().split():
        match = re.match(r"^(\^|~|>=|>)?(.*)$", comparator)
        if match is None:
            continue
        operator = match.group(1) or ""
        target = semver.parse_version(match.group(2).strip())
        if target is None:
            continue
        if operator in ("^", "~", ">=", ""):
            if lower is None or semver.compare_version(target, lower) > 0:
                lower = target
        elif operator == ">":
            if lower is None or semver.compare_version(target, lower) > 0:
                lower = target
    return lower


def classify_failure(host_version: str, range_text: str) -> str:
    """Direction of the failure — mirrors ``classifyPeer`` of ``dshmarket`` (risk vs warning).

    ``above-implicit-ceiling`` is NOT treated as a definite incompatibility: the
    ecosystem is full of ranges like ``^0.0.1`` whose upper bound was never meant
    to be a ceiling on the host.
    """
    raw = range_text.strip()
    if semver.parse_version(raw) is not None:
        return "exact-pin"
    has_explicit_upper = "<" in raw
    lowers = []
    for alternative in raw.split("||"):
        bound = _alternative_lower_bound(alternative)
        if bound is None:
            return "above-implicit-ceiling"
        lowers.append(bound)
    host = semver.parse_version(host_version)
    if host is not None and all(semver.compare_version(host, bound) < 0 for bound in lowers):
        return "below-min"
    if has_explicit_upper:
        return "above-explicit-max"
    return "above-implicit-ceiling"


def evaluate(host_version: str | None, declarations: list[Declaration]) -> Verdict:
    """Verdict from the declarations — the analogue of ``dshmarket``'s ``deriveHostCompatibility``."""
    if not declarations:
        return Verdict(STATUS_UNKNOWN, "undeclared")
    requirement = " ∩ ".join(sorted({d.range for d in declarations}))
    if host_version is None:
        return Verdict(STATUS_UNKNOWN, "manifest", requirement, declarations)

    for declaration in declarations:
        result = semver.satisfies(host_version, declaration.range)
        declaration.result = result
        if result is False:
            declaration.direction = classify_failure(host_version, declaration.range)

    definite = [d for d in declarations if d.result is False and d.direction in DEFINITE_FAILURES]
    if definite:
        return Verdict(STATUS_INCOMPATIBLE, "manifest", requirement, declarations)
    if all(d.result is True for d in declarations):
        return Verdict(STATUS_COMPATIBLE, "manifest", requirement, declarations)
    return Verdict(STATUS_UNKNOWN, "manifest", requirement, declarations)


# --------------------------------------------------------------------------- #
# Code source for the scans: a directory or a tarball
# --------------------------------------------------------------------------- #

def client_relative_names(manifest: dict | None) -> list[str]:
    """Relative names of the plugin client bundles (candidates)."""
    names: list[str] = []
    if isinstance(manifest, dict):
        exports = manifest.get("exports")
        if isinstance(exports, dict):
            client = exports.get("./client")
            target = client.get("default") if isinstance(client, dict) else client
            if isinstance(target, str):
                names.append(target)
    names += ["client.js", "lib/client.js", "dist/client.js"]
    normalized = []
    for name in names:
        normalized.append(name[2:] if name.startswith("./") else name)
    return normalized


@dataclass
class CodeSource:
    """Plugin code suitable for scanning: files in memory + client bundles.

    A plugin directory and a local tarball are reduced to the same shape, so
    checks 2 and 3 behave identically for an installed plugin and for an artifact
    that is yet to be installed.
    """

    files: dict[str, str] = field(default_factory=dict)
    client_files: set[str] = field(default_factory=set)
    origin: str = ""

    @property
    def empty(self) -> bool:
        return not self.files

    @classmethod
    def from_directory(cls, directory: Path, manifest: dict | None = None,
                       *, skip_parts: frozenset[str] = SKIP_PARTS) -> CodeSource:
        """Collect code from a directory (an installed plugin or a repository link)."""
        root = Path(directory)
        files: dict[str, str] = {}
        for suffix in CODE_SUFFIXES:
            for path in sorted(root.rglob(f"*{suffix}")):
                if len(files) >= MAX_FILES:
                    break
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                parts = relative.parts[:-1]
                if any(part in skip_parts or part.startswith(".") for part in parts):
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    files[relative.as_posix()] = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
        source = cls(files=files, origin=str(root))
        source.client_files = {name for name in client_relative_names(manifest) if name in files}
        return source

    def specifiers(self, *, only_client: bool = False) -> dict[str, set[str]]:
        """External specifiers actually required by the code."""
        names = self.client_files if only_client else set(self.files)
        found: dict[str, set[str]] = {}
        for relative in sorted(names):
            text = self.files.get(relative)
            if text is None:
                continue
            for specifier in _REQUIRE_RE.findall(text):
                if specifier.startswith(".") or specifier.startswith("node:"):
                    continue
                if not _is_module_specifier(specifier):
                    continue
                found.setdefault(specifier, set()).add(relative)
        return found


def _as_code_source(value) -> CodeSource:
    """Accept either a ready CodeSource or a plugin directory."""
    return value if isinstance(value, CodeSource) else CodeSource.from_directory(Path(value))


def client_bundle_files(plugin_dir: Path) -> list[Path]:
    """Built client bundles of a plugin (usually ``client.js``)."""
    directory = Path(plugin_dir)
    manifest = None
    try:
        manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    seen: list[Path] = []
    for relative in client_relative_names(manifest):
        candidate = directory / relative
        if candidate.is_file() and candidate not in seen:
            seen.append(candidate)
    return seen


def required_specifiers(source, *, only_client: bool = False) -> dict[str, set[str]]:
    """External specifiers actually required by the plugin code.

    Returns ``{specifier: {files}}``. Relative paths and node: modules are
    dropped. ``source`` is a plugin directory or a :class:`CodeSource`.
    """
    return _as_code_source(source).specifiers(only_client=only_client)


def scan_removed_packages(source, removed: set[str]) -> list[dict]:
    """Hard links to packages that are gone from the target core version."""
    hits = []
    for specifier, files in sorted(_as_code_source(source).specifiers().items()):
        base = "/".join(specifier.split("/")[:2]) if specifier.startswith("@") else specifier
        if base in removed:
            hits.append({"specifier": specifier, "package": base, "files": sorted(files)[:4]})
    return hits


def scan_client_modules(source, seed_words: set[str], known_packages: set[str]) -> list[dict]:
    """``require`` in the client bundle of names missing from the target version.

    The client half resolves requests through the browser module table; a miss
    throws "missed the module table". Only client bundles are checked.
    """
    hits = []
    for specifier, files in sorted(_as_code_source(source).specifiers(only_client=True).items()):
        base = "/".join(specifier.split("/")[:2]) if specifier.startswith("@") else specifier
        if base in ("react", "react-dom"):
            continue  # always present in the seed table
        if specifier in seed_words or base in seed_words:
            continue
        if base in known_packages:
            continue  # the package exists in the target version — the graph edge is probably there
        hits.append({"specifier": specifier, "files": sorted(files)[:4]})
    return hits


# --------------------------------------------------------------------------- #
# Factory registrations inside a client bundle
# --------------------------------------------------------------------------- #

# The registration facade every browser bundle is wrapped in. Tolerant of
# formatting and minification; ``[^}]`` spans newlines, so the one-property-per-line
# form tsdown emits matches as well.
_REGISTRATION_RE = re.compile(
    r"__ModuleLoader__\s*\.\s*load\s*\(\s*\{[^}]*?\bid\s*:\s*['\"]([^'\"\n]+)['\"]",
)


def registrations_of(text: str) -> list[str]:
    """Factory ids a bundle source registers through ``window.__ModuleLoader__.load``."""
    return _REGISTRATION_RE.findall(text)


def scan_client_registrations(source, package_name: str | None) -> list[dict]:
    """Client bundles that break the browser module table at boot.

    Two failures of one family — both leave the declarations and every "does the
    package exist" scan green while ``dsh`` does not start:

    * **foreign registration** — the bundle registers a factory for ANOTHER
      package. A self-registering client bundle was inlined instead of being
      required from the host (a type-only import with inline ``type`` modifiers,
      ``import { type A } from '<pkg>/client'``, is the usual cause). The host
      already owns that row, so the loader throws
      ``client-modules: duplicate factory registration for "<id>"``.
    * **duplicate registration** — one bundle installs more than one factory.

    ``package_name`` is the plugin's own manifest name; without it only the
    duplicate half of the check can run.
    """
    code = _as_code_source(source)
    # Only a DECLARED client bundle is ever executed by the loader, so that is
    # where the facade call can be observed. Scanning every file instead would
    # flag documentation and build scripts that merely quote the pattern.
    names = sorted(code.client_files) if code.client_files else sorted(code.files)
    hits = []
    for relative in names:
        text = code.files.get(relative)
        if text is None:
            continue
        ids = registrations_of(text)
        if not ids:
            continue
        foreign = [item for item in ids if package_name is not None and item != package_name]
        duplicated = len(ids) > 1
        if not foreign and not duplicated:
            continue
        hits.append({
            "file": relative,
            "ids": ids,
            "foreign": foreign,
            "duplicate": duplicated,
        })
    return hits


# --------------------------------------------------------------------------- #
# Declaration integrity (check 4) and inline purity (check 5)
# --------------------------------------------------------------------------- #

#: A bundle that inlines a module is marked with the physical file it came from.
#: rolldown/tsdown emit one ``//#region`` per inlined module, so the marker set is
#: the honest record of WHAT was inlined. A hand-written bundle (esbuild or plain
#: JS) has no markers at all — then this check simply has nothing to see, which is
#: why it never turns a clean verdict into a false one.
_REGION_RE = re.compile(r"^[ \t]*//#region[ \t]+(\S.*?)[ \t]*$", re.M)

_NODE_MODULES = "node_modules/"


def _inlined_modules(text: str) -> list[tuple[str, str]]:
    """``(package, subpath)`` for every module a bundle inlined.

    Both layouts occur: the pnpm store form
    (``node_modules/.pnpm/@scope+name@1.2.3/node_modules/@scope/name/lib/x.js``)
    and a flat ``node_modules/@scope/name/...``. The LAST ``node_modules/``
    occurrence is the package root in either case.
    """
    found: list[tuple[str, str]] = []
    for path in _REGION_RE.findall(text):
        index = path.rfind(_NODE_MODULES)
        if index < 0:
            continue
        parts = path[index + len(_NODE_MODULES):].split("/")
        if not parts or not parts[0] or parts[0].startswith("."):
            continue
        if parts[0].startswith("@") and len(parts) > 1:
            name, rest = f"{parts[0]}/{parts[1]}", parts[2:]
        elif parts[0].startswith("@"):
            continue
        else:
            name, rest = parts[0], parts[1:]
        entry = (name, "/".join(rest))
        if entry not in found:
            found.append(entry)
    return found


def inlined_packages(text: str) -> list[str]:
    """npm packages a bundle inlined, read from its ``//#region`` markers."""
    names: list[str] = []
    for name, _ in _inlined_modules(text):
        if name not in names:
            names.append(name)
    return names


def _string_array(subject: str, field: str, value):
    """Port of the core's ``optionalStringArray``: ``(items | None, error | None)``."""
    if value is None:
        return None, None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None, f"{subject} {field} must be a string array"
    return value, None


def client_declaration(manifest: dict | None) -> dict | None:
    """The plugin's ``dsh.client`` object (``None`` when it declares none)."""
    if not isinstance(manifest, dict):
        return None
    section = manifest.get("dsh")
    if not isinstance(section, dict):
        return None
    declaration = section.get("client")
    return declaration if isinstance(declaration, dict) else None


def declared_client_externals(manifest: dict | None) -> set[str]:
    """``dsh.client.external`` as declared (exact specifiers, never normalized)."""
    declaration = client_declaration(manifest)
    if declaration is None:
        return set()
    items, _ = _string_array("", "dsh.client.external", declaration.get("external"))
    return set(items or ())


def _package_root(specifier: str) -> str:
    """``@scope/name/sub`` → ``@scope/name``; ``name/sub`` → ``name``."""
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def external_base_names(manifest: dict | None) -> set[str]:
    """Package roots of ``dsh.client.external`` — the rows this plugin requests.

    A declared request like ``@deepseek-ai/dsh-api-gateway/client`` permits the
    whole package to be external, so the base name is what the inline check has to
    allow.
    """
    return {_package_root(specifier) for specifier in declared_client_externals(manifest)}


def scan_client_declaration(manifest: dict | None, source: "CodeSource | None" = None, *,
                            package_name: str | None = None,
                            require_bundle: bool = False) -> list[dict]:
    """Check 4 — the ``dsh.client`` declaration and the bundle it promises.

    The host composes the boot graph from this declaration and then fetches
    ``exports["./client"]``. Every failure here is loud at boot and invisible
    everywhere else: the declarations are satisfied, every package exists, types
    check, and the plugin still does not load. The rules are the host's own
    (``dsh-client-modules``): a non-object ``dsh.client``, a non-string
    ``platform``, a non-string-array ``inject``/``external``, a non-boolean
    ``immediately``, a declared client with no ``./client`` export, an
    ``exports["./client"]`` that is neither a string nor ``{ default: string }``,
    a row that requests its own package, and a promised bundle that is not in the
    artifact.

    ``require_bundle`` is set for a packed artifact (a tarball or the installed
    copy). A ``link:`` to a repository legitimately has no built bundle yet, so
    the missing-file rule is skipped there instead of turning into a false hit.
    """
    section = manifest.get("dsh") if isinstance(manifest, dict) else None
    if not isinstance(section, dict) or "client" not in section:
        return []  # not a client plugin at all
    declaration = client_declaration(manifest)
    subject = package_name or (manifest.get("name") if isinstance(manifest, dict) else None) or "package"
    if declaration is None:
        # ``dsh.client`` is present but not an object: the host throws on it.
        return [{
            "kind": "shape",
            "field": "dsh.client",
            "message": f"{subject} has a non-object dsh.client declaration",
            "files": [],
        }]

    hits: list[dict] = []

    if not isinstance(declaration.get("platform"), str):
        hits.append({
            "kind": "shape",
            "field": "dsh.client.platform",
            "message": f"{subject} dsh.client.platform must be a string",
            "files": [],
        })
    for field in ("inject", "external"):
        _, error = _string_array(subject, f"dsh.client.{field}", declaration.get(field))
        if error:
            hits.append({"kind": "shape", "field": f"dsh.client.{field}", "message": error, "files": []})
    immediately = declaration.get("immediately")
    if immediately is not None and not isinstance(immediately, bool):
        hits.append({
            "kind": "shape",
            "field": "dsh.client.immediately",
            "message": f"{subject} dsh.client.immediately must be a boolean",
            "files": [],
        })

    exports = manifest.get("exports") if isinstance(manifest, dict) else None
    client_export = exports.get("./client") if isinstance(exports, dict) else None
    target: str | None = None
    if client_export is None:
        hits.append({
            "kind": "missing-export",
            "field": 'exports["./client"]',
            "message": f'{subject} declares dsh.client but exports no "./client"',
            "files": [],
        })
    elif isinstance(client_export, str):
        target = client_export
    elif isinstance(client_export, dict) and isinstance(client_export.get("default"), str):
        target = client_export["default"]
    else:
        hits.append({
            "kind": "export-shape",
            "field": 'exports["./client"]',
            "message": f'{subject} exports["./client"] must be a string or {{ default: string }}',
            "files": [],
        })

    for specifier in sorted(declared_client_externals(manifest)):
        if _package_root(specifier) == subject:
            hits.append({
                "kind": "self-external",
                "field": "dsh.client.external",
                "message": f"{subject} requests its own package in dsh.client.external",
                "files": [],
            })

    if require_bundle and target is not None and source is not None:
        relative = target[2:] if target.startswith("./") else target
        if relative not in source.files:
            hits.append({
                "kind": "missing-bundle",
                "field": target,
                "message": (f"{subject}: client bundle not found at {target} — "
                            "run the package build before packing"),
                "files": [],
            })
    return hits


@dataclass
class InlinePolicy:
    """What a client bundle may inline in ONE target core version.

    The build-time mirror of the module-edge rules (the core's
    ``dsh-client-bundle-purity`` gate): the shell baseline and the package's own
    ``dsh.client.external`` stay external, a few wire layers are inline-safe, and
    everything else under ``@deepseek-ai/`` must come from the module table. An
    artifact built under a DIFFERENT rule set — a stale prebuilt tarball — can
    inline a package that now carries shared runtime identity, and nothing fails
    loudly: the duplicate instance simply does not match by ``Symbol``/
    ``instanceof``/singleton, so a panel stays empty.
    """

    platform: set[str] = field(default_factory=set)
    client_rows: set[str] = field(default_factory=set)
    inline_safe: re.Pattern | None = None
    vendored: re.Pattern | None = None
    generated_remote: re.Pattern | None = None
    source: str = ""

    def allows(self, package: str, own_external: set[str] = frozenset(),
               subpath: str = "") -> bool:
        """May a bundle inline ``package`` (optionally the given in-package file)?"""
        if package in self.platform or package in own_external:
            return True
        if self.vendored is not None and self.vendored.match(package):
            return True
        if self.inline_safe is not None and self.inline_safe.match(package):
            return True
        # A generated ``/remote`` contribution has no shared identity and is the
        # point of inlining. Only the module actually read may claim this: the
        # exemption must NEVER be granted for a package as a whole, or every
        # ``@deepseek-ai/dsh-*`` package would silently become inline-safe.
        if self.generated_remote is not None and "remote" in subpath:
            return True
        return False


def scan_inline_purity(source, policy: InlinePolicy, package_name: str | None = None,
                       own_external: set[str] = frozenset()) -> list[dict]:
    """Check 5 — a client bundle inlining a package that must be a module-table row.

    Two outcomes, both bad and both silent to the other checks:

    * the inlined package declares ``dsh.client`` of its own, so the host loads it
      as a separate graph row — and the bundle carries a SECOND copy with its own
      module state;
    * the inlined package is not inline-safe in this target version at all, so the
      duplicate instance is not interchangeable with the host's.

    Only the DECLARED client bundle is read: that is the artifact the loader
    executes.
    """
    code = _as_code_source(source)
    hits: list[dict] = []
    for relative in sorted(code.client_files):
        text = code.files.get(relative)
        if text is None:
            continue
        for package, subpath in _inlined_modules(text):
            if not package.startswith("@deepseek-ai/"):
                continue
            if package_name is not None and package == package_name:
                continue
            if policy.allows(package, own_external, subpath):
                continue
            row = package in policy.client_rows
            hits.append({
                "package": package,
                "file": relative,
                "client_row": row,
                "why": ("inlines a package the host loads as its own client row "
                        "(a second copy of its module state)" if row else
                        "inlines a package that is neither a module-table row nor "
                        "inline-safe in the target version"),
            })
    return hits


#: Fallback classification, copied from core 0.1.5-rc.2 when no checkout is
#: available. Kept deliberately BROAD: a missing entry would turn a legitimate
#: inline into a false incompatibility, while an extra entry only relaxes a check
#: that still fires on the unambiguous signal — an inlined client row.
BUILTIN_PLATFORM_MODULES: tuple[str, ...] = (
    "react", "react/jsx-runtime", "react-dom", "react-dom/client", "@deepseek-ai/cordis",
    "@deepseek-ai/dsh-client-store",
    "@deepseek-ai/dsh-client-ui-slots",
    "@deepseek-ai/dsh-client-ui-primitives",
    "@deepseek-ai/dsh-client-ui-dockkit",
)

BUILTIN_INLINE_SAFE = (
    r"^(?:@deepseek-ai\/dsh-(?:file-reference|session|llm|tools|brand|deque|output-retention"
    r"|typert-protocol|util-crypto|util-values|util-workspace-path)(?:\/|$)"
    r"|@deepseek-ai\/dsh-token-meter\/client$"
    r"|@deepseek-ai\/dsh-host-open-in-app\/shared$"
    r"|@deepseek-ai\/dsh-agent-presets\/display$"
    r"|@deepseek-ai\/dsh-spill-policy\/notice$)"
)

BUILTIN_VENDORED_LIBRARY = r"^@deepseek-ai\/(cosmokit|schemastery)(\/|$)"

BUILTIN_GENERATED_REMOTE = r"^@deepseek-ai\/dsh-[a-z0-9]+(?:-[a-z0-9]+)*\/remote$"


def compile_classification(inline_safe: str | None, vendored: str | None,
                           generated_remote: str | None) -> tuple:
    """Compile the three patterns, falling back to the built-in 0.1.5-rc.2 set.

    A pattern that does not compile in Python (a JS-only construct in a future
    core) falls back rather than crashing: the check must never be the reason an
    upgrade audit fails.
    """
    compiled = []
    for text, fallback in ((inline_safe, BUILTIN_INLINE_SAFE),
                           (vendored, BUILTIN_VENDORED_LIBRARY),
                           (generated_remote, BUILTIN_GENERATED_REMOTE)):
        candidates = [text, fallback] if text else [fallback]
        pattern = None
        for candidate in candidates:
            try:
                pattern = re.compile(candidate)
                break
            except re.error:
                continue
        compiled.append(pattern)
    return tuple(compiled)
