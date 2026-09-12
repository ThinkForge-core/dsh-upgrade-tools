"""Prerelease-aware semver arithmetic (stdlib only).

Why a home-grown implementation: the marketplace compatibility engine lives in
the ``dshmarket`` package (https://github.com/dsh-market/dsh-market), which is
detached together with the other plugins for
the duration of an upgrade, and it runs on Node. The same slice of semantics is
needed here in pure Python, with ``includePrerelease: true`` behaviour — that is
exactly how the marketplace counts
(``satisfiesRange(hostVersion, declared, { includePrerelease: true })``).
Without it the range ``^0.1.1-rc.2`` would not cover ``0.1.5-rc.1``, which is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_PARTIAL_RE = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$")
_COMPARATOR_RE = re.compile(r"^(\^|~|>=|<=|>|<)?\s*(.+)$")


@dataclass(frozen=True)
class Version:
    """A parsed version."""

    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if not self.pre else base + "-" + ".".join(self.pre)


def parse_version(value: object) -> Version | None:
    """Parse a version; None when it is not a version."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    return Version(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre)


def _compare_pre(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1  # a release outranks its own prerelease
    if not right:
        return -1
    for index in range(max(len(left), len(right))):
        if index >= len(left):
            return -1
        if index >= len(right):
            return 1
        a, b = left[index], right[index]
        a_num, b_num = a.isdigit(), b.isdigit()
        if a_num and b_num:
            if int(a) != int(b):
                return -1 if int(a) < int(b) else 1
            continue
        if a_num != b_num:
            return -1 if a_num else 1
        if a != b:
            return -1 if a < b else 1
    return 0


def compare_version(left: Version | str, right: Version | str) -> int:
    """Compare two versions: -1 / 0 / 1."""
    a = parse_version(left) if isinstance(left, str) else left
    b = parse_version(right) if isinstance(right, str) else right
    if a is None or b is None:
        raise ValueError(f"compare_version: not a version: {left!r} / {right!r}")
    for name in ("major", "minor", "patch"):
        x, y = getattr(a, name), getattr(b, name)
        if x != y:
            return -1 if x < y else 1
    return _compare_pre(a.pre, b.pre)


def _upper_bound(operator: str, target: Version) -> Version | None:
    if operator == "^":
        if target.major > 0:
            return Version(target.major + 1, 0, 0, ("0",))
        if target.minor > 0:
            return Version(0, target.minor + 1, 0, ("0",))
        return Version(0, 0, target.patch + 1, ("0",))
    if operator == "~":
        return Version(target.major, target.minor + 1, 0, ("0",))
    return None


def _match_comparator(version: Version, comparator: str) -> bool | None:
    """True/False, or None when the comparator cannot be parsed (uncertainty)."""
    raw = comparator.strip()
    if raw in ("", "*", "x", "X"):
        return True
    match = _COMPARATOR_RE.match(raw)
    operator = (match.group(1) or "") if match else ""
    target_text = (match.group(2) or "").strip() if match else raw

    exact = parse_version(target_text)
    if exact is None:
        partial = _PARTIAL_RE.match(target_text)
        if partial is None or operator != "":
            return None  # ranges like "1.x - 2.x" are deliberately unsupported
        actual = (version.major, version.minor, version.patch)
        for index, part in enumerate(partial.groups()):
            if part is None or part in ("x", "X", "*"):
                break
            if int(part) != actual[index]:
                return False
        return True

    order = compare_version(version, exact)
    if operator == "":
        return order == 0
    if operator == ">=":
        return order >= 0
    if operator == ">":
        return order > 0
    if operator == "<=":
        return order <= 0
    if operator == "<":
        return order < 0
    if operator in ("^", "~"):
        if order < 0:
            return False
        upper = _upper_bound(operator, exact)
        return upper is not None and compare_version(version, upper) < 0
    return None


def satisfies(version: str, range_text: str, include_prerelease: bool = True) -> bool | None:
    """Does the version satisfy the range?

    ``||`` and space separated comparators are supported (``^``, ``~``, ``>=``,
    ``>``, ``<=``, ``<``, an exact version, ``*``). Returns None when the range
    cannot be parsed — that is uncertainty, not a failure.
    """
    parsed = parse_version(version)
    if parsed is None or not isinstance(range_text, str) or not range_text.strip():
        return None

    for alternative in range_text.split("||"):
        comparators = [part for part in alternative.strip().split() if part]
        if not comparators:
            continue
        matched = True
        unknown = False
        for comparator in comparators:
            result = _match_comparator(parsed, comparator)
            if result is None:
                unknown = True
                break
            if not result:
                matched = False
                break
        if unknown:
            return None
        if not matched:
            continue
        if not parsed.pre or include_prerelease:
            return True
        # Strict mode: a prerelease passes only with a comparator of the same tuple.
        tuple_text = f"{parsed.major}.{parsed.minor}.{parsed.patch}"
        if any(tuple_text in c and "-" in c for c in comparators):
            return True
    return False


def max_satisfying(versions, range_text: str, include_prerelease: bool = True) -> str | None:
    """The highest version from the list that satisfies the range."""
    best: str | None = None
    for candidate in versions:
        if satisfies(candidate, range_text, include_prerelease) is not True:
            continue
        if best is None or compare_version(candidate, best) > 0:
            best = candidate
    return best
