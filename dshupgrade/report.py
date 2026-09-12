"""Terminal reports: width-aware tables, grouped verdicts and summaries.

The tables here never truncate a cell. Text that does not fit into its column is
wrapped onto continuation lines, and those lines are padded so they stay aligned
under their own column instead of being pushed to the left margin. Column widths
are computed from the content first and only then squeezed to the terminal width,
and the widest column (usually ``reason``) is the one that gives way first.
"""

from __future__ import annotations

from . import style
from .compat import STATUS_COMPATIBLE, STATUS_INCOMPATIBLE, STATUS_UNKNOWN

#: Short status marks used in every table.
GLYPH = {
    STATUS_COMPATIBLE: "ok",
    STATUS_INCOMPATIBLE: "NO",
    STATUS_UNKNOWN: "??",
}

#: Human readable status names.
STATUS_LABEL = {
    STATUS_COMPATIBLE: "compatible",
    STATUS_INCOMPATIBLE: "incompatible",
    STATUS_UNKNOWN: "unconfirmed",
}

#: ANSI style per status.
STATUS_STYLE = {
    STATUS_COMPATIBLE: "green",
    STATUS_INCOMPATIBLE: "bold,red",
    STATUS_UNKNOWN: "yellow",
}

#: Order in which status groups are printed (worst first).
STATUS_ORDER = (STATUS_INCOMPATIBLE, STATUS_UNKNOWN, STATUS_COMPATIBLE)

GAP = 2          # spaces between columns
FLOOR = 6        # a column never gets narrower than this
SOFT_MAX = 14    # columns above this width may be squeezed down to FLOOR


def status_glyph(status: str | None) -> str:
    """Colored status mark for a table cell."""
    mark = GLYPH.get(status or "", "??")
    return style.paint(mark, *STATUS_STYLE.get(status or "", "yellow").split(","))


def status_name(status: str | None) -> str:
    """Colored human readable status name."""
    label = STATUS_LABEL.get(status or "", "unconfirmed")
    return style.paint(label, *STATUS_STYLE.get(status or "", "yellow").split(","))


def group_title(status: str | None, count: int) -> str:
    """Title of a status group in the verdict table."""
    mark = GLYPH.get(status or "", "??")
    style_name = STATUS_STYLE.get(status or "", "yellow")
    return style.paint(f"{mark} — {STATUS_LABEL.get(status or '', 'unconfirmed')} ({count})",
                       *style_name.split(","))


def short_version(value: str | None) -> str:
    return value if value else "—"


def _as_text(cell) -> str:
    return "" if cell is None else str(cell)


def _cell_lines(cell, column_width: int) -> list[str]:
    """Wrap one cell (possibly multi-line) into physical lines.

    A cell that already fits is returned untouched, so an escape sequence inside
    it survives; only text that has to be broken is wrapped as plain text.
    """
    text = _as_text(cell)
    if "\n" not in text and style.width(text) <= column_width:
        return [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(style.wrap(paragraph, column_width))
    return lines or [""]


def _pad(text: str, column_width: int) -> str:
    padding = column_width - style.width(text)
    return text + " " * padding if padding > 0 else text


def _natural_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [style.width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index >= len(widths):
                continue
            for line in _as_text(cell).split("\n"):
                widths[index] = max(widths[index], style.width(line))
    return widths


def _fit_widths(widths: list[int], available: int, gap: int, floor: int) -> list[int]:
    """Squeeze natural widths into ``available`` columns.

    The shrink is proportional to how much room a column can give up, so a column
    that needs to be wide (an ``reason`` or a long path) stays relatively wider
    than a narrow one. A column never goes below ``floor``; if even that does not
    fit, the table is rendered wider than the terminal rather than unreadable.
    """
    def total(values: list[int]) -> int:
        return sum(values) + gap * (len(values) - 1)

    current = list(widths)
    if total(current) <= available or not current:
        return current
    limits = [max(floor, min(value, SOFT_MAX)) for value in current]
    for _ in range(len(current) * 4 + 8):
        deficit = total(current) - available
        if deficit <= 0:
            break
        headroom = [max(0, current[index] - limits[index]) for index in range(len(current))]
        available_headroom = sum(headroom)
        if available_headroom <= 0:
            # Every column already sits at its limit: relax the widest one.
            widest = max(range(len(current)), key=lambda index: current[index])
            if current[widest] <= floor:
                break
            limits[widest] = floor
            continue
        for index, room in enumerate(headroom):
            if not room:
                continue
            share = -(-deficit * room // available_headroom)  # ceil, so it converges
            current[index] -= min(room, share)
    return current


def _row_block(row: list[str], widths: list[int], gap: int, indent: int) -> list[str]:
    """Render one logical row as one or more physical lines."""
    cells = [
        _cell_lines(row[index] if index < len(row) else "", widths[index])
        for index in range(len(widths))
    ]
    height = max(len(cell) for cell in cells) if cells else 1
    separator = " " * gap
    prefix = " " * indent
    lines = []
    for line_index in range(height):
        parts = [
            _pad(cell[line_index] if line_index < len(cell) else "", widths[index])
            for index, cell in enumerate(cells)
        ]
        lines.append((prefix + separator.join(parts)).rstrip())
    return lines


def _group_rule(label: str, total_width: int, indent: int) -> str:
    """Separator line with the group title embedded into it."""
    prefix = " " * indent
    head = style.paint("──", "dim") + " " + label + " "
    fill = max(0, total_width - indent - style.width(head))
    return (prefix + head + style.paint("─" * fill, "dim")).rstrip()


def _render(headers: list[str], sections, *, width: int | None = None,
            indent: int = 0, gap: int = GAP, floor: int = FLOOR,
            header: bool = True) -> str:
    """Render a table from ``[(group label | None, rows), …]`` sections."""
    rows = [row for _, group in sections for row in group]
    available = (width if width is not None else style.terminal_width()) - indent
    widths = _fit_widths(_natural_widths(headers, rows), available, gap, floor)
    total_width = indent + sum(widths) + gap * (len(widths) - 1)
    prefix = " " * indent
    lines: list[str] = []
    if header:
        lines.append((prefix + (" " * gap).join(
            _pad(style.subheading(name), widths[index]) for index, name in enumerate(headers)
        )).rstrip())
        lines.append(prefix + (" " * gap).join(style.paint("-" * value, "dim")
                                                for value in widths))
    for label, group in sections:
        if label:
            lines.append(_group_rule(label, total_width, indent))
        for row in group:
            lines.extend(_row_block(row, widths, gap, indent))
    return "\n".join(lines)


def table(headers: list[str], rows: list[list[str]], **options) -> str:
    """A plain table: header, rule, then rows (wrapped, never truncated)."""
    return _render(headers, [(None, rows)], **options)


def grouped_table(headers: list[str], groups, **options) -> str:
    """A table split into labelled groups sharing one set of column widths.

    ``groups`` is ``[(label, rows), …]``; an empty group is skipped.
    """
    sections = [(label, rows) for label, rows in groups if rows]
    if not sections:
        return ""
    return _render(headers, sections, **options)


# --------------------------------------------------------------------------- #
# Notes and blocks
# --------------------------------------------------------------------------- #

def bullet(text: str, *, indent: int = 2, mark: str = "·") -> str:
    """A hanging-indent bullet, wrapped to the terminal width."""
    prefix = " " * indent
    continuation = prefix + " " * (len(mark) + 1)
    lines = style.wrap_lines(text, max(20, style.terminal_width() - indent - len(mark) - 1))
    out = [f"{prefix}{style.dim(mark)} {lines[0]}"]
    out.extend(f"{continuation}{line}" for line in lines[1:])
    return "\n".join(out)


def warning(text: str, *, indent: int = 2) -> str:
    """A highlighted warning line with a hanging indent."""
    prefix = " " * indent
    lines = style.wrap_lines(text, max(20, style.terminal_width() - indent - 2))
    out = [f"{prefix}{style.warn('!')} {style.warn(lines[0])}"]
    out.extend(f"{prefix}  {style.warn(line)}" for line in lines[1:])
    return "\n".join(out)


def detail(text: str, *, indent: int = 4, mark: str = "") -> str:
    """A wrapped detail line with an optional leading mark.

    ``mark`` may carry ANSI styling: it is measured with the escapes stripped, and
    only the first physical line shows it, the continuations line up under the
    text.
    """
    mark_width = style.width(mark)
    head = (mark + " ") if mark else ""
    continuation = " " * (mark_width + 1) if mark else ""
    room = max(20, style.terminal_width() - indent - mark_width - 1)
    lines = style.wrap_lines(text, room)
    return "\n".join(
        " " * indent + (head if index == 0 else continuation) + line
        for index, line in enumerate(lines)
    )


def analysis_rows(plugins: list[dict]) -> list[list[str]]:
    """Rows of the compatibility matrix, one per plugin."""
    rows = []
    for plugin in plugins:
        version = plugin.get("localVersion") or plugin.get("version")
        if plugin.get("localVersion") and plugin.get("version") \
                and plugin["localVersion"] != plugin["version"]:
            version = f"{plugin['version']}→{plugin['localVersion']}"
        rows.append([
            status_glyph(plugin["status"]),
            plugin["name"],
            short_version(version),
            plugin.get("sourceLabel") or plugin.get("source") or "—",
            (plugin.get("latest") or "—") if plugin.get("latest") else "—",
            plugin.get("reason") or "",
        ])
    return rows


def analysis_groups(plugins: list[dict]) -> list[tuple[str, list[list[str]]]]:
    """The same rows, split into status groups (worst first)."""
    by_status: dict[str, list[dict]] = {}
    for plugin in plugins:
        by_status.setdefault(plugin["status"], []).append(plugin)
    groups = []
    for status in STATUS_ORDER:
        items = by_status.get(status) or []
        if items:
            groups.append((group_title(status, len(items)), analysis_rows(items)))
    return groups


def print_counts(counts: dict[str, int]) -> None:
    """One-line verdict summary, colored per status."""
    parts = [
        style.paint(f"compatible: {counts.get(STATUS_COMPATIBLE, 0)}", "green"),
        style.paint(f"incompatible: {counts.get(STATUS_INCOMPATIBLE, 0)}", "bold", "red"),
        style.paint(f"unconfirmed: {counts.get(STATUS_UNKNOWN, 0)}", "yellow"),
    ]
    print("  " + "   ".join(parts))


def print_unknown_hint(analysis) -> None:
    """Explain what "unconfirmed" does NOT mean, once per report.

    A plugin that declares no DSH version cannot be compared to one — a gap in the
    declarations, not a failure. When the target IS the installed core, the runtime
    verdict ("does the installed copy import") is available from ``verify`` and shown
    by ``status``.
    """
    unconfirmed = [plugin for plugin in analysis.plugins if plugin["status"] == STATUS_UNKNOWN]
    if not unconfirmed:
        return
    print()
    print(bullet("unconfirmed = nothing declared to compare, not a failure: the manifest "
                 "says nothing about DSH versions."))
    if analysis.current_core is not None and analysis.current_core == analysis.target:
        undeclared = [plugin for plugin in unconfirmed
                      if plugin.get("code_clean") and plugin.get("installed")]
        if undeclared:
            print(bullet(f"{len(undeclared)} of them are installed on this core with clean "
                         "code — 'verify' imports their copies, and 'status' shows the result "
                         "in its 'loads' column."))


def print_analysis(analysis, *, verbose: bool = False, width: int | None = None) -> None:
    """Print the compatibility matrix: header notes, grouped table, summary."""
    print()
    print(style.heading(f"=== Compatibility with core {analysis.target} ==="))
    for note in analysis.notes:
        print(bullet(note))
    if analysis.removed:
        print(bullet(f"removed packages: {', '.join(analysis.removed)}"))
    if analysis.preloaded is not None and not analysis.preloaded:
        print(bullet("PRELOADED_CLIENT_EXTERNALS is empty in the target version"))
    missing_local = [plugin["name"] for plugin in analysis.plugins
                     if plugin.get("installable") is False]
    if missing_local:
        print(warning("local artifact not found (reinstallation impossible): "
                      + ", ".join(missing_local)))

    headers = ["", "plugin", "version", "source", "latest", "reason"]
    print()
    print(grouped_table(headers, analysis_groups(analysis.plugins), width=width))

    counts = analysis.counts()
    print()
    print_counts(counts)
    print_unknown_hint(analysis)

    if verbose:
        print_verbose(analysis, width=width)


def print_verbose(analysis, *, width: int | None = None) -> None:
    """Per-plugin detail blocks: declarations, hard links, client bundle hits."""
    shown = 0
    for plugin in analysis.plugins:
        has_local = bool(plugin.get("local"))
        if (not plugin["declarations"] and not plugin.get("removed_hits")
                and not plugin.get("client_hits") and not plugin.get("registration_hits")
                and not plugin.get("declaration_hits") and not plugin.get("inline_hits")
                and not has_local):
            continue
        shown += 1
        title = (f"{plugin['name']} ({short_version(plugin.get('version'))}) "
                 f"[{status_name(plugin['status'])}]")
        print()
        print("  " + style.paint("──", "dim") + " " + style.subheading(title))
        if has_local:
            local = plugin["local"]
            print(detail(f"local source: {local.get('spec')}"))
            artifact = f"{local.get('kind')} {local.get('path')}"
            if not local.get("available"):
                artifact += "   " + style.bad("NOT FOUND")
            print(detail(f"local artifact: {artifact}"))
            if local.get("version"):
                print(detail(f"artifact version: {local['version']}"))
            if plugin.get("code_origin"):
                print(detail(f"code scanned from: {plugin['code_origin']}"))
            if plugin.get("npmLatest"):
                twin_status = plugin.get("npmLatestStatus") or "?"
                print(detail(style.warn(
                    f"npm has {plugin['npmLatest']} under this name ({twin_status}) — "
                    "a DIFFERENT artifact; the local one is installed by specifier")))
        for declaration in plugin["declarations"]:
            result = declaration["result"]
            mark = {True: "ok", False: "NO", None: "??"}.get(result, "??")
            if result is True:
                styled = style.paint(mark, "green")
            elif result is False:
                styled = style.paint(mark, "bold", "red")
            else:
                styled = style.paint(mark, "yellow")
            direction = declaration["direction"] or ""
            target = declaration["package"] or "@deepseek-ai/dsh"
            print(detail(f"[{styled}] {declaration['source']}: "
                         f"{target} {declaration['range']} {direction}"))
        for hit in plugin.get("declaration_hits") or []:
            print(detail(style.paint("[NO]", "bold", "red")
                         + f" dsh.client: {hit['field']} — {hit['message']}"))
        for hit in plugin.get("inline_hits") or []:
            print(detail(style.paint("[NO]", "bold", "red")
                         + f" inlined module: {hit['package']} ← {hit['file']} — {hit['why']}"
                         + (" [host client row]" if hit.get("client_row") else "")))
        for hit in plugin.get("removed_hits") or []:
            print(detail(style.paint("[NO]", "bold", "red")
                         + f" removed package: {hit['specifier']} ← {', '.join(hit['files'])}"))
        for hit in plugin.get("client_hits") or []:
            print(detail(style.paint("[NO]", "bold", "red")
                         + f" client module: {hit['specifier']} ← {', '.join(hit['files'])}"))
        for hit in plugin.get("registration_hits") or []:
            registered = ", ".join(hit["ids"]) or "—"
            print(detail(style.paint("[NO]", "bold", "red")
                         + f" factory registration: {hit['file']} registers {registered}"
                         + (f"; another package's row: {', '.join(hit['foreign'])}"
                            if hit["foreign"] else "")
                         + " (the loader allows one factory per bundle: "
                           "duplicate factory registration)"))
    if shown:
        print()
        print(style.dim(f"  detail blocks: {shown}"))
