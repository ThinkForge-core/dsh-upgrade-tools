"""Does the plugin *do* anything — or does it only import?

:mod:`dshupgrade.verify` answers "the entry imported". That is necessary, not
sufficient: a plugin can import perfectly and still have no effect at all. Two
shapes of that failure are invisible to an import probe and to the declaration
scans:

**The row never applies.**
A cordis plugin whose declared ``inject`` names a service the deployment does not
provide is never applied at all — the loader leaves its fiber ``pending`` and
``apply()`` is simply not called. Nothing it would have registered exists, while
the import is spotless.

**The surface it draws into is gone.**
A client half that augments another component's DOM (or fills its slot) keeps
working only while that component is mounted. When the effective profile
disables or replaces that row, the client half loads and then does nothing, in
silence — its only trace is a ``console.debug`` in a browser console nobody
reads. The row it appends to is rendered by a core component such as
``@deepseek-ai/dsh-client-ui-workspace``, and a profile layer that disables that
row leaves it with nothing to draw into.

To see the second one, this module reads the **effective loader configuration**:
the bundle patches of every mounted bundle, in bundle order, then the profile's
own ``cordis.patch.yml``. From that it learns which rows exist, which are
disabled, and *who disabled them* — then cross-references that against what each
plugin's client half actually touches, so "this plugin draws into a component you
switched off" becomes a printed fact with a file and a line, instead of a
mystery.

Scope, stated honestly:

* The patch files are read as the restricted structure DSH actually uses — a
  top-level list of entries, each either ``insert: [...]`` or an id-targeted
  override. YAML semantics (anchors, flow collections, multiline scalars) are
  not implemented; only the keys that decide enablement are read, and a
  ``!!js`` expression is reported as *conditional* rather than guessed at.
* A row disabled with a ``!!js`` expression is **not** claimed to be disabled —
  the loader decides, and this module has no loader. It is reported as
  conditional, with the expression.
* The authoritative answer to "did the row actually apply" is the running
  deployment's own plugin inventory (the GUI's Settings → Plugins, served by
  ``@deepseek-ai/dsh-host-plugin-inventory`` as ``pluginInventory.list``). This
  module is the offline approximation, and the live HTTP probe in
  :mod:`dshupgrade.verify` is the empirical confirmation of the parts that
  register routes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .paths import core_install_dir, host_modules_dir, read_json

# --------------------------------------------------------------------------- #
# The patch subset
# --------------------------------------------------------------------------- #

#: Entry keys that decide what the loader tree looks like. Everything else in an
#: entry (``config`` and its whole subtree) is deliberately not read.
_ENTRY_KEYS = ("id", "name", "disabled", "insert")


@dataclass
class Op:
    """One patch operation, in file order."""

    kind: str                     # "mount" (an insert row) or "set" (id-targeted)
    id: str
    name: str | None = None
    enabled: bool | None = True   # None = conditional (a !!js expression)
    condition: str | None = None  # the expression, when conditional

    @property
    def conditional(self) -> bool:
        return self.enabled is None


@dataclass
class Row:
    """A loader row after every layer has been applied to it."""

    id: str
    name: str
    enabled: bool | None = True
    condition: str | None = None
    mounted_by: str = ""
    disabled_by: str | None = None
    updated_by: list[str] = field(default_factory=list)

    @property
    def conditional(self) -> bool:
        return self.enabled is None

    @property
    def disabled(self) -> bool:
        return self.enabled is False


@dataclass
class Effective:
    """The loader tree this profile boots into."""

    rows: dict[str, Row] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    #: Module specifier -> the enabled row ids mounting it (a real double-mount).
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    #: Id-targeted overrides that named an id no layer ever mounted.
    orphans: list[str] = field(default_factory=list)

    def enabled(self) -> list[Row]:
        return [self.rows[rid] for rid in self.order if self.rows[rid].enabled is True]

    def disabled(self) -> list[Row]:
        return [self.rows[rid] for rid in self.order if self.rows[rid].disabled]

    def conditional(self) -> list[Row]:
        return [self.rows[rid] for rid in self.order if self.rows[rid].conditional]

    def by_name(self, name: str) -> Row | None:
        for rid in self.order:
            if self.rows[rid].name == name:
                return self.rows[rid]
        return None

    def find_disabled_token(self, token: str) -> Row | None:
        """A disabled row whose id or module specifier carries ``token``."""
        for row in self.disabled():
            if row.id == token or row.name == token:
                return row
            if row.name.rsplit("/", 1)[-1] == token:
                return row
        return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def strip_comment(line: str) -> str:
    """Drop a ``#`` comment, honouring quotes (``'#'`` inside a name is data)."""
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def scalar(text: str) -> tuple[str, bool]:
    """Unquote a scalar; the flag marks a ``!!js`` expression."""
    value = (text or "").strip()
    if value.startswith("!!js"):
        return value[4:].strip(), True
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1], False
    return value, False


def enablement(raw: str | None) -> tuple[bool | None, str | None]:
    """Read a ``disabled:`` value as (enabled, condition)."""
    if raw is None or not raw.strip():
        return True, None
    value, is_expression = scalar(raw)
    if is_expression:
        return None, value
    lowered = value.lower()
    if lowered in ("false", "no", "off"):
        return True, None
    if lowered in ("true", "yes", "on"):
        return False, None
    return None, value


def _list_items(lines: list[str], dash_indent: int) -> list[list[str]]:
    """Split lines into ``- `` items whose dash sits at ``dash_indent``."""
    items: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if not line.strip():
            continue
        if _indent(line) == dash_indent and line.strip().startswith("- "):
            current = [line.strip()[2:]]
            items.append(current)
            continue
        if current is not None and _indent(line) > dash_indent:
            current.append(line)
    return items


def _item_fields(item: list[str], dash_indent: int) -> dict[str, str]:
    """The ``key: value`` pairs of one list item, at the item's own level."""
    fields: dict[str, str] = {}
    key, _, value = item[0].partition(":")
    fields[key.strip()] = value.strip()
    for line in item[1:]:
        if _indent(line) != dash_indent + 2:
            continue
        key, _, value = line.strip().partition(":")
        fields.setdefault(key.strip(), value.strip())
    return fields


def parse_patch(text: str) -> list[Op]:
    """The operations of one patch file, in order."""
    lines = [strip_comment(line) for line in (text or "").splitlines()]
    ops: list[Op] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or _indent(line) != 0:
            index += 1
            continue
        if not line.strip().startswith("- "):
            index += 1
            continue
        # One top-level entry: the dash content plus every deeper line after it.
        block = [line.strip()[2:]]
        index += 1
        while index < len(lines):
            following = lines[index]
            if not following.strip():
                index += 1
                continue
            if _indent(following) == 0:
                break
            block.append(following)
            index += 1
        ops.extend(_entry_ops(block))
    return ops


def _entry_ops(block: list[str]) -> list[Op]:
    """One entry: an ``insert`` list, or an id-targeted override."""
    fields = _item_fields(block, 0)
    if not fields.get("id") and any(key in fields for key in _ENTRY_KEYS):
        # An insert (or an empty entry) — rows live in its nested list.
        ops: list[Op] = []
        for item in _list_items(block[1:], 4):
            row = _item_fields(item, 4)
            row_id = row.get("id") or (row.get("name") or "").split("/")[-1]
            if not row_id:
                continue
            enabled, condition = enablement(row.get("disabled"))
            ops.append(Op("mount", row_id.strip("'\""), (row.get("name") or row_id).strip("'\""),
                          enabled, condition))
        return ops
    row_id = (fields.get("id") or "").strip("'\"")
    if not row_id:
        return []
    name_raw = fields.get("name")
    name = None if name_raw is None else scalar(name_raw)[0]
    enabled, condition = enablement(fields.get("disabled"))
    return [Op("set", row_id, name, enabled, condition)]


# --------------------------------------------------------------------------- #
# Resolving the profile
# --------------------------------------------------------------------------- #

def _package_dir(name: str, profile_dir: Path, install_dir: Path | None) -> Path | None:
    """The directory of a package a mounted row names.

    The profile's own tree comes first (that is what the profile actually
    resolves); the installed core's tree is the fallback, read in whichever
    layout it uses (see :func:`paths.host_modules_dir`).
    """
    candidates = [profile_dir / "node_modules" / name]
    if install_dir is not None:
        candidates.append(Path(install_dir) / "node_modules" / name)
        modules = host_modules_dir(install_dir)
        if modules is not None:
            candidates.append(modules / name)
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    return None


def patch_layer(name: str, profile_dir: Path, install_dir: Path | None) -> tuple[str, Path] | None:
    """The bundle patch file a mounted bundle declares, if any."""
    directory = _package_dir(name, profile_dir, install_dir)
    if directory is None:
        return None
    manifest = read_json(directory / "package.json")
    if not isinstance(manifest, dict):
        return None
    bundle = (manifest.get("dsh") or {}).get("bundle") or {}
    relative = bundle.get("patch")
    if isinstance(relative, str) and relative.strip():
        path = directory / relative
        return (name, path) if path.is_file() else None
    default = directory / "cordis.patch.yml"
    if default.is_file():
        return name, default
    return None


def layers(profile, install_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Every patch layer of the profile, in the order the loader merges them."""
    if install_dir is None:
        install_dir = core_install_dir()
    directory = Path(profile.directory)
    found: list[tuple[str, Path]] = []
    for bundle in profile.bundles:
        layer = patch_layer(bundle, directory, install_dir)
        if layer is not None:
            found.append(layer)
    own = directory / "cordis.patch.yml"
    if own.is_file():
        found.append(("(profile cordis.patch.yml)", own))
    return found


def resolve(profile, install_dir: Path | None = None) -> Effective:
    """Apply every layer in order and return the effective loader tree."""
    effective = Effective()
    for label, path in layers(profile, install_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        effective.layers.append(label)
        for op in parse_patch(text):
            _apply(effective, op, label)
    _cross_check(effective)
    return effective


def _apply(effective: Effective, op: Op, layer: str) -> None:
    row = effective.rows.get(op.id)
    if op.kind == "mount":
        if row is None:
            effective.rows[op.id] = Row(
                id=op.id,
                name=op.name or op.id,
                enabled=op.enabled,
                condition=op.condition,
                mounted_by=layer,
                disabled_by=layer if op.enabled is False else None,
            )
            effective.order.append(op.id)
            return
        # A second mount of the same id: the loader fails loudly on this, so it is
        # reported as a duplicate while keeping the first row's identity.
        row.updated_by.append(layer)
        return
    if row is None:
        if op.id not in effective.orphans:
            effective.orphans.append(op.id)
        return
    row.updated_by.append(layer)
    if op.name:
        row.name = op.name
    if op.enabled is False:
        row.enabled = False
        row.condition = op.condition
        row.disabled_by = layer
    elif op.enabled is True:
        row.enabled = True
        row.condition = None
        row.disabled_by = None
    else:
        row.enabled = None
        row.condition = op.condition
        row.disabled_by = layer


def _cross_check(effective: Effective) -> None:
    """Rows that mount the same module: a real double-mount, not a warning."""
    mounts: dict[str, list[str]] = {}
    for row in effective.enabled():
        mounts.setdefault(row.name, []).append(row.id)
    effective.duplicates = {name: ids for name, ids in mounts.items() if len(ids) > 1}


# --------------------------------------------------------------------------- #
# What each plugin's client half touches
# --------------------------------------------------------------------------- #

#: Client-half behaviours that cannot be confirmed without a browser.
_DOM_MARKERS = ("MutationObserver", "querySelector", "createElement", "document.")


@dataclass
class ClientHalf:
    """What a plugin's browser bundle is made of, as far as text can tell."""

    entry: Path | None = None
    text: str = ""
    injects: list[str] = field(default_factory=list)
    platform: str | None = None
    #: True when the bundle reaches into the DOM rather than a public slot.
    dom: bool = False
    selectors: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.entry is not None


#: `role="menu"`, `[role=treeitem]` (the unquoted CSS form) and
#: `data-slot="conversation.session.header"` alike.
_SELECTOR_RE = re.compile(
    r"""(?:role|data-slot|data-[a-z-]+)\s*=\s*\\?["']?([^"'\s\]\\]{1,60})""")


def client_entry(plugin_dir: Path) -> Path | None:
    """The file the web runtime loads as this plugin's client half."""
    directory = Path(plugin_dir)
    manifest = read_json(directory / "package.json")
    if not isinstance(manifest, dict):
        return None
    exports = manifest.get("exports")
    if isinstance(exports, dict):
        for key in ("./client", "./client/service", "./client/api"):
            value = exports.get(key)
            relative = value.get("default") if isinstance(value, dict) else value
            if isinstance(relative, str) and (directory / relative).is_file():
                return directory / relative
    client = ((manifest.get("dsh") or {}).get("client")) or {}
    for key in ("entry", "module", "file"):
        relative = client.get(key)
        if isinstance(relative, str) and (directory / relative).is_file():
            return directory / relative
    default = directory / "client.js"
    return default if default.is_file() else None


def client_half(plugin_dir: Path, manifest: dict | None = None) -> ClientHalf:
    """Read a plugin's client half: its entry, injects and DOM footprint."""
    directory = Path(plugin_dir)
    manifest = manifest if isinstance(manifest, dict) else read_json(directory / "package.json")
    client = ((manifest or {}).get("dsh") or {}).get("client") or {}
    half = ClientHalf(
        injects=[str(item) for item in (client.get("inject") or [])],
        platform=client.get("platform"),
    )
    entry = client_entry(directory)
    if entry is None:
        return half
    half.entry = entry
    try:
        half.text = entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return half
    half.dom = any(marker in half.text for marker in _DOM_MARKERS)
    half.selectors = sorted({match for match in _SELECTOR_RE.findall(half.text)})
    return half


#: A shadow that is a verdict, because the manifest itself names the module.
DECLARED = "declared"
#: A shadow that is a lead: the row's name only appears in the client half's text.
REFERENCE = "reference"


@dataclass
class Shadow:
    """A host surface a plugin needs that the effective profile switched off.

    ``kind`` separates the two findings. A ``declared`` shadow is a verdict:
    ``package.json``'s ``dsh.client.inject`` names a module whose loader row is
    off, so the client half cannot be composed. A ``reference`` shadow is a lead:
    the disabled row's id or module name occurs in the client half's text — a
    comment naming the component it augments, say — and the client half only *may*
    need it.
    """

    plugin: str
    token: str          # the row id / module the client half names
    row: Row            # the disabled row that owns it
    evidence: str       # "client.js:5: The row ⋮ menu is rendered by ..."
    route_count: int = 0
    kind: str = REFERENCE
    #: The enabled row that re-mounts the same DOM contract, when one does.
    replaced_by: str | None = None

    @property
    def certain(self) -> bool:
        """True when the manifest, not a text match, produced this finding."""
        return self.kind == DECLARED

    @property
    def explained(self) -> bool:
        """True when another enabled row reproduces the DOM contract it needs."""
        return self.replaced_by is not None

    @property
    def confidence(self) -> str:
        """``verdict`` when the manifest produced the finding, ``lead`` otherwise."""
        return "verdict" if self.certain else "lead"

    def to_dict(self) -> dict:
        return {
            "plugin": self.plugin,
            "token": self.token,
            "row": self.row.id,
            "module": self.row.name,
            "disabled_by": self.row.disabled_by,
            "evidence": self.evidence,
            "route_count": self.route_count,
            "kind": self.kind,
            "replaced_by": self.replaced_by,
            "confidence": self.confidence,
        }


def shadow_flags(items: list[Shadow]) -> tuple[bool, bool]:
    """``(certain, suspect)`` for one plugin's findings.

    The column has one cell, so the two kinds cannot both be printed in it. A
    declared shadow keeps the plain ``shadowed``; a code reference becomes
    ``shadowed?`` because it is a lead, and disappears from the column entirely
    when an enabled row replaces the surface it named.
    """
    certain = any(item.certain for item in items)
    suspect = any(not item.certain and not item.explained for item in items)
    return certain, suspect


#: `class*="sessionRow"` — the hashed-class contract a client half selects on.
_CLASS_CONTRACT_RE = re.compile(r"""class\*=\\?["']([A-Za-z0-9_-]{3,})""")
#: `[role="menu"]`, `[role=treeitem]`, `[data-slot="x"]` selector attributes.
_ATTR_CONTRACT_RE = re.compile(
    r"""\[(?:role|data-slot|data-[a-z-]+)\s*=\s*\\?["']?([^"'\s\]\\]{3,})""")


def dom_contract(half: ClientHalf) -> set[str]:
    """The DOM names a client half selects on — its contract with the surface.

    These are the names a replacement has to reproduce: a plugin that looks for
    ``[class*="sessionRow"]`` and ``[role="treeitem"]`` does not care which package
    renders them.
    """
    if not half.text:
        return set()
    return set(_CLASS_CONTRACT_RE.findall(half.text)) | set(_ATTR_CONTRACT_RE.findall(half.text))


def replacement_for(plugin, effective: Effective, half: ClientHalf, row: Row,
                    profile_dir: Path | None = None) -> str | None:
    """The enabled row that re-mounts the surface ``row`` switched off, if any.

    A profile layer that disables a stock UI row usually mounts its own replacement
    in the same layer — ``sample-replacement`` disables ``ui-workspace`` and
    inserts its own session list. When that replacement's client half carries every
    DOM name the consumer selects on, the consumer's reference to the disabled row
    describes history, not breakage: the module is gone, the contract is not.
    """
    if not row.disabled_by:
        return None
    contract = dom_contract(half)
    if not contract:
        return None
    base = Path(profile_dir) if profile_dir is not None else Path(plugin.directory).parent.parent
    for other in effective.enabled():
        if other.mounted_by != row.disabled_by or other.name == plugin.name:
            continue
        directory = _package_dir(other.name, base, core_install_dir())
        if directory is None:
            continue
        candidate = client_half(directory)
        if not candidate.text:
            continue
        if all(name in candidate.text for name in contract):
            return other.name
    return None


def blank_line_strings(line: str) -> str:
    """Erase the contents of quoted literals on ONE line.

    A name inside a user-facing message is prose, not a dependency: the Chinese
    hint "…（skill-filesystem 会热扫描）…" in a client bundle's locale table says
    nothing about that plugin needing ``skill-filesystem``.

    This works line by line on purpose. Blanking a whole file needs a real JS
    lexer: an unpaired apostrophe in an English comment (``web app's module
    loader``) would otherwise open a "string" that swallows everything after it —
    and the comment on line 5 is precisely the evidence worth finding.
    """
    # Most lines carry no literal at all, and the caller walks megabytes of code:
    # the scan over the characters is only worth starting when there is a quote.
    if "'" not in line and '"' not in line and "`" not in line:
        return line
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for char in line:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
                out.append(char)
                continue
            out.append(" ")
            continue
        if char in "'\"`":
            quote = char
            out.append(char)
            continue
        out.append(char)
    return "".join(out)


def _is_comment_line(stripped: str) -> bool:
    """A line whose own text is a comment — quotes in it are just apostrophes."""
    return stripped.startswith(("//", "/*", "*"))


def _snippet(stripped: str) -> str:
    """The evidence text of one line, clipped to something a terminal can hold."""
    snippet = " ".join(stripped.split())
    return snippet[:93] + "…" if len(snippet) > 96 else snippet


def _evidence_lines(text: str, tokens, folder: str) -> dict[str, str]:
    """First line naming each token, found in ONE walk over the client bundle.

    A bundle is a file of source and the scan asks about a whole loader layer's
    worth of names, so the lines are split and de-quoted once and every candidate is
    tested during the same walk. A plain substring test runs first and the boundary
    pattern only after it, which keeps the regex count near zero on the lines that
    cannot match anyway.
    """
    wanted = set(tokens)
    patterns = {
        token: re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(token) + r"(?![A-Za-z0-9_-])")
        for token in wanted
    }
    found: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        haystack = line if _is_comment_line(stripped) else blank_line_strings(line)
        for token in list(wanted):
            if token not in haystack or not patterns[token].search(haystack):
                continue
            found[token] = f"{folder}:{number}: {_snippet(stripped)}"
            wanted.discard(token)
        if not wanted:
            break
    return found


def is_replacer(plugin, effective: Effective, row: Row) -> bool:
    """True when this very plugin is what switched the surface off.

    ``sample-replacement`` disables the ``ui-workspace`` row *and* names it in
    its own labels — it replaced that component, so it plainly does not depend on
    it. A row mounted by the same layer that disabled the surface means exactly
    that, and no text analysis is needed.
    """
    if not row.disabled_by:
        return False
    if plugin.name == row.disabled_by:
        return True
    for other in effective.rows.values():
        if other.mounted_by != row.disabled_by:
            continue
        if other.name == plugin.name or other.name.startswith(plugin.name + "/"):
            return True
    return False


def shadows_for(plugin, effective: Effective, *, route_count: int = 0,
                profile_dir: Path | None = None) -> list[Shadow]:
    """Disabled host surfaces the installed client half talks about.

    Three conditions must hold together, which is what keeps this honest:

    1. the client half actually builds on a UI surface (it touches the DOM, names
       a ``role``/``data-slot`` selector, or declares injected client modules) —
       a pure server plugin is never reported here;
    2. the disabled row's id or module name appears in its code or comments, not
       inside a user-facing string;
    3. the plugin is not itself the thing that disabled that row.

    This is a text match, so what it produces is a **lead**, not a verdict: the
    finding carries :data:`REFERENCE` and the line that satisfied (2) is printed
    with it, so the claim can be checked by eye. It stays a lead unless another
    enabled row in the disabling layer reproduces the DOM contract the client half
    selects on (see :func:`replacement_for`) — then it is recorded as explained and
    the consumer's mention of the disabled row describes history, not breakage.
    """
    if not plugin.installed or not plugin.directory:
        return []
    directory = Path(plugin.directory)
    half = client_half(directory)
    if not half.present or not half.text:
        return []
    if not (half.dom or half.selectors or half.injects):
        return []
    folder = half.entry.name if half.entry else "client.js"

    # Both the row id and the module's own name are worth looking for; the module
    # specifier is the stronger of the two. A name the bundle does not contain at
    # all is dropped before the walk — the walk is the expensive part.
    candidates: list[tuple[Row, str]] = []
    seen: set[str] = set()
    for row in effective.disabled():
        if is_replacer(plugin, effective, row):
            continue
        for token in (row.id, row.name, row.name.rsplit("/", 1)[-1]):
            if not token or token in seen or len(token) < 4:
                continue
            seen.add(token)          # the same name always yields the same evidence line
            if token in half.text:
                candidates.append((row, token))
    if not candidates:
        return []

    evidence = _evidence_lines(half.text, [token for _, token in candidates], folder)
    found: list[Shadow] = []
    reported: set[int] = set()
    for row, token in candidates:
        line = evidence.get(token)
        if not line or id(row) in reported:
            continue
        reported.add(id(row))        # one finding per disabled row, as the walk order asks
        found.append(Shadow(plugin.name, token, row, line, route_count,
                            kind=REFERENCE,
                            replaced_by=replacement_for(plugin, effective, half, row,
                                                        profile_dir)))
    return found


def declared_shadows(plugin, effective: Effective) -> list[Shadow]:
    """The exact case: an injected client module whose loader row is disabled.

    No heuristics here — the manifest names the module and the effective
    configuration says its row is off, so the client half cannot be composed.
    """
    if not plugin.installed or not plugin.directory:
        return []
    half = client_half(Path(plugin.directory))
    found: list[Shadow] = []
    for module in half.injects:
        row = effective.by_name(module)
        if row is None or not row.disabled:
            continue
        found.append(Shadow(plugin.name, module, row,
                            f"package.json: dsh.client.inject names {module}", 0,
                            kind=DECLARED))
    return found


def scan_shadows(profile, effective: Effective, *, route_counts: dict[str, int] | None = None
                 ) -> dict[str, list[Shadow]]:
    """Shadowed surfaces for every installed plugin of the profile.

    Every finding carries its own kind (see :class:`Shadow`): a declared shadow is
    certain, a code-reference shadow is a lead. Explained leads stay in the map —
    dropping them would hide why the column says nothing — but
    :func:`shadow_flags` keeps them out of the ``shadowed`` cell.
    """
    counts = route_counts or {}
    result: dict[str, list[Shadow]] = {}
    for plugin in profile.plugins:
        found = declared_shadows(plugin, effective)
        covered = {shadow.row.id for shadow in found}
        found.extend(shadow for shadow in shadows_for(plugin, effective,
                                                      route_count=counts.get(plugin.name, 0),
                                                      profile_dir=Path(profile.directory))
                     if shadow.row.id not in covered)
        if found:
            result[plugin.name] = found
    return result
