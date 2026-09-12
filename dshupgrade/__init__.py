"""DeepSeek Harness upgrade tools.

Modules:

* :mod:`dshupgrade.paths` — where things live (DSH home, profile, installed core, checkouts).
* :mod:`dshupgrade.config` — the settings file: options remembered between runs.
* :mod:`dshupgrade.completion` — terminal-grade path input (Tab completion, unescaping).
* :mod:`dshupgrade.semver` — prerelease-aware semver arithmetic.
* :mod:`dshupgrade.registry` — the npm registry and the marketplace index.
* :mod:`dshupgrade.locals` — local builds (``file:``/``link:``/tgz): manifest and code from the artifact.
* :mod:`dshupgrade.host` — host package inventory of the installed core.
* :mod:`dshupgrade.compat` — the six compatibility checks.
* :mod:`dshupgrade.checkout` — core version checkout and facts from its tree.
* :mod:`dshupgrade.profile` — reading the profile, detaching and installing plugins.
* :mod:`dshupgrade.snapshot` — snapshots and incompatible lists.
* :mod:`dshupgrade.analysis` — folding the verdicts into one report.
* :mod:`dshupgrade.report` — table output.
* :mod:`dshupgrade.style` — colors, terminal width, wrapping.
"""

__all__ = ["analysis", "checkout", "compat", "completion", "config", "host", "locals", "paths",
           "profile", "registry", "report", "semver", "snapshot", "style"]
__version__ = "0.1.0"
