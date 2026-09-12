"""Stable identifiers for the findings the checks report.

Every finding carries a human readable reason and, beside it, an identifier from
this module. The reason is written to be read and may be reworded; the identifier
is written to be compared, so a consumer can branch on the class of a failure
without parsing prose.

A plugin may fail more than one check at once, while a report field holds one
value: :func:`primary` reduces a set of identifiers to the one that names the
most fundamental failure. The order in :data:`PRIORITY` runs from a failure that
is independent of the target version (it breaks on any core) to the weakest
signal — a manifest that declares nothing to compare with.
"""

from __future__ import annotations

#: A peer dependency range does not admit the target version.
PEER_RANGE_MISMATCH = "PEER_RANGE_MISMATCH"
#: ``engines.dsh`` does not admit the target version.
ENGINES_DSH_MISMATCH = "ENGINES_DSH_MISMATCH"
#: The code hard-links a package the target version no longer ships.
REMOVED_PACKAGE_REQUIRED = "REMOVED_PACKAGE_REQUIRED"
#: The client bundle requires a name missing from the browser module table.
BROWSER_MODULE_TABLE_MISS = "BROWSER_MODULE_TABLE_MISS"
#: A client bundle registers a factory id the host already owns, or two of them.
DUPLICATE_FACTORY_REGISTRATION = "DUPLICATE_FACTORY_REGISTRATION"
#: The ``dsh.client`` declaration, or the bundle it promises, is not loadable.
DECLARATION_INTEGRITY_FAILURE = "DECLARATION_INTEGRITY_FAILURE"
#: A client bundle inlines a package that must come from the module table.
INLINE_PURITY_VIOLATION = "INLINE_PURITY_VIOLATION"
#: A literal RPC path is not an endpoint the target core serves.
WIRE_ENDPOINT_DEAD = "WIRE_ENDPOINT_DEAD"
#: The path is served, but the envelope's own method disagrees with it.
WIRE_METHOD_MISMATCH = "WIRE_METHOD_MISMATCH"
#: A route handler fails on its first call (a ReferenceError in its closure).
HANDLER_REFERENCE_ERROR = "HANDLER_REFERENCE_ERROR"
#: The manifest declares no DSH version — there is nothing to compare against.
UNKNOWN_DECLARATIONS = "UNKNOWN_DECLARATIONS"

#: Every identifier, ordered by the severity of what it names.
PRIORITY = (
    DECLARATION_INTEGRITY_FAILURE,
    DUPLICATE_FACTORY_REGISTRATION,
    REMOVED_PACKAGE_REQUIRED,
    BROWSER_MODULE_TABLE_MISS,
    INLINE_PURITY_VIOLATION,
    PEER_RANGE_MISMATCH,
    ENGINES_DSH_MISMATCH,
    WIRE_ENDPOINT_DEAD,
    WIRE_METHOD_MISMATCH,
    HANDLER_REFERENCE_ERROR,
    UNKNOWN_DECLARATIONS,
)

#: Wire verdict (``dshupgrade.wire``) to identifier.
WIRE = {
    "dead": WIRE_ENDPOINT_DEAD,
    "mismatch": WIRE_METHOD_MISMATCH,
}


def primary(candidates) -> str | None:
    """The most fundamental identifier among ``candidates`` (or None).

    Unknown values are ignored, so a caller may pass every identifier it guessed
    at without filtering. An empty result means no check produced a finding that
    has an identifier.
    """
    found = {candidate for candidate in candidates if candidate}
    for code in PRIORITY:
        if code in found:
            return code
    return None


def wire_codes(verdicts) -> list[str]:
    """Identifiers for a sequence of wire verdicts (``dead``/``mismatch``)."""
    return [WIRE[verdict] for verdict in verdicts if verdict in WIRE]
