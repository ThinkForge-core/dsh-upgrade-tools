"""Interactive menu: every CLI capability, available without arguments.

Running ``python3 dsh_upgrade.py`` without arguments opens this menu. The rules:

* **stdlib only** — no new dependencies, the menu runs wherever the rest of the
  tool runs (a plain Python with DSH switched off).
* **One and the same logic.** The menu does not duplicate the commands: it builds
  the same arguments and calls the same ``cmd_*`` functions as the CLI. It cannot
  drift apart from the command line.
* **Destructive work goes through a preview.** Detaching, installing and the
  pipeline are always shown first in ``--dry-run`` mode, and only then ask for
  confirmation with the word ``yes``.
* **Settings are remembered.** What the settings screen changes is written to the
  settings file and applies to every later run (a command-line flag always wins
  for the run it was given on).
* **Paths are typed with a terminal, not into a void.** Where the input is a real
  terminal, path prompts get Tab completion, the usual line editing and history
  (see :mod:`dshupgrade.completion`).
* **No hanging without a TTY.** When stdin is not a terminal (a script, a pipe,
  cron), the menu prints the action map and exits with code 0.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path

from . import completion, config as config_mod, effects as effects_mod, invocation as invocation_mod
from . import paths, registry, snapshot, style, wire as wire_mod
from .paths import core_install_dir, core_version, profile_dir
from .profile import read_profile
from .report import GLYPH, grouped_table

MENU_WIDTH = 74


def line() -> str:
    return style.paint("─" * MENU_WIDTH, "dim")


class MenuExit(Exception):
    """The user left the menu (or closed the input)."""


@dataclass
class Settings:
    """Current run parameters — the same thing as the common CLI flags."""

    profile: str = "web"
    core: str | None = None
    offline: bool = False
    no_clone: bool = False
    json_output: bool = False
    color: str = "auto"
    verbose: bool = True
    state_dir: str | None = None
    #: Where version checkouts live: ``temp`` (default), ``keep`` or a directory.
    checkouts: str = paths.TEMP
    #: Options that came from a command-line flag: they are never written to the
    #: settings file, so a one-off flag cannot silently become permanent.
    pinned: frozenset = frozenset()
    # Cache of the automatic target: the header is rendered on every menu loop,
    # so the version is resolved once per session (and per offline mode).
    _auto_target: str | None = field(default=None, compare=False, repr=False)
    _auto_offline: bool | None = field(default=None, compare=False, repr=False)
    _auto_fallback: bool = field(default=False, compare=False, repr=False)

    # ------------------------------------------------------------- persistence
    def as_config(self) -> dict:
        """The settings as the settings file spells them (``--flag`` names)."""
        return {
            "profile": self.profile,
            "core": self.core,
            "offline": self.offline,
            "no_clone": self.no_clone,
            "json": self.json_output,
            "verbose": self.verbose,
            "color": self.color,
            "state_dir": self.state_dir,
            "checkouts": self.checkouts,
        }

    def apply_config(self, values: dict) -> None:
        """Take options from ``values`` — never over a pinned (command-line) one."""
        for name, value in values.items():
            if name in self.pinned:
                continue
            if name == "json":
                self.json_output = bool(value)
            elif name == "color":
                # An unset colour means "auto" in the menu (never the literal None).
                self.color = style.normalize_mode(value)
            elif hasattr(self, name):
                setattr(self, name, value)

    def save(self) -> Path | None:
        """Write the settings file; None when it could not be written."""
        payload = {name: value for name, value in self.as_config().items()
                   if name not in self.pinned}
        try:
            return config_mod.save(payload)
        except OSError:
            return None

    def checkouts_label(self) -> str:
        """A human sentence for the checkout location."""
        if self.checkouts == paths.TEMP:
            return (f"temporary ({paths.temp_checkouts_root()}) — reused between runs, "
                    "cleared by the OS on reboot")
        if self.checkouts == paths.KEEP:
            return f"kept in {paths.dsh_home() / 'checkouts'}"
        return f"kept in {self.checkouts}"

    def auto_target(self, *, refresh: bool = False) -> str:
        """The version the automatic target resolves to: the NEWEST published one.

        The header calls this on every loop, so by default the answer comes from
        the registry cache (offline reads never block). ``refresh=True`` — used
        when the user actually asks about the target — goes to the network.

        When no version can be determined at all (no cache, no network), the
        installed core is shown instead and marked as a fallback.
        """
        cached = self._auto_target is not None and self._auto_offline == self.offline
        if not refresh and cached:
            return self._auto_target
        resolved = registry.newest_core_version(offline=self.offline or not refresh)
        self._auto_fallback = resolved is None
        self._auto_target = resolved or core_version() or "not found"
        self._auto_offline = self.offline
        return self._auto_target

    def target_label(self, *, refresh: bool = False) -> str:
        """``auto — 0.1.5-rc.2``, or the version chosen explicitly."""
        if self.core:
            return self.core
        version = self.auto_target(refresh=refresh)
        suffix = " (installed fallback)" if self._auto_fallback else ""
        return f"auto — {version}{suffix}"

    def mode_label(self) -> str:
        parts = ["offline" if self.offline else "online"]
        if self.no_clone:
            parts.append("no clone")
        return ", ".join(parts)


class Console:
    """Menu input/output (streams are injectable for the tests)."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout

    # ---------------------------------------------------------------- painting
    def colored(self) -> bool:
        return style.enabled(self.stdout)

    def paint(self, text: object, *styles: str) -> str:
        """Style text only when colors are on for this console."""
        return style.paint(text, *styles) if self.colored() else style.strip(str(text))

    # ------------------------------------------------------------------- input
    def write(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def ask(self, prompt: str, default: str = "") -> str:
        self.stdout.write(self.paint(prompt, "cyan") if prompt else prompt)
        self.stdout.flush()
        answer = self.stdin.readline()
        if answer == "":
            raise MenuExit
        answer = answer.strip()
        return answer if answer else default

    def ask_yes(self, prompt: str, *, default: bool = False) -> bool:
        marker = "Y/n" if default else "y/N"
        answer = self.ask(f"{prompt} [{marker}]: ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes", "1", "true")

    def interactive(self) -> bool:
        """True when this console is the real terminal (readline can take over)."""
        return (self.stdin is sys.stdin and self.stdout is sys.stdout
                and bool(getattr(sys.stdin, "isatty", lambda: False)()))

    def ask_path(self, prompt: str, default: str = "") -> str:
        """Read a path, with terminal-grade entry where that is possible.

        On a real terminal the reader is wired to ``readline``: Tab completes
        files and directories (directories get a trailing slash, spaces are
        escaped, ``file:``/``link:`` specifiers are completed after the prefix),
        arrows/Home/End edit the line and up-arrow recalls earlier paths. The
        returned value is a plain path — quotes, backslash escapes and ``~`` are
        resolved. Without a terminal (a pipe or a test) this is the ordinary
        reader, so nothing changes for scripts.
        """
        if not self.interactive():
            return completion.prepare(self.ask(prompt, default))
        completion.install()
        try:
            answer = input(prompt)  # readline handles the editing and completion
        except EOFError:
            raise MenuExit from None
        if not answer.strip():
            return default
        completion.remember(answer)
        return completion.prepare(answer)

    def confirm_yes(self, prompt: str) -> bool:
        answer = self.ask(f"{prompt}\n  type yes to confirm: ").strip().lower()
        return answer == "yes"

    def pause(self) -> None:
        self.ask("\nEnter — back to the menu ")


@dataclass
class Action:
    """A menu item."""

    key: str
    title: str
    handler: str
    danger: bool = False
    hint: str = ""


ACTIONS: tuple[Action, ...] = (
    Action("1", "Status: core, tags, profile plugins", "status",
           hint="fast: cache and profile files only, nothing is executed"),
    Action("2", "Core versions: what the registry offers", "core_versions"),
    Action("3", "Plan: new core versions and what happens to plugins", "plan",
           hint="quick, declarations only"),
    Action("4", "Check: full compatibility matrix", "check",
           hint="checkout of the target + code scans"),
    Action("5", "Snapshot and detach ALL plugins", "detach", danger=True),
    Action("6", "Install plugins from a snapshot", "attach", danger=True),
    Action("7", "Recheck the incompatible list", "recheck"),
    Action("8", "Update plugins that have a newer version (core untouched)", "plugins",
           danger=True, hint="nothing else in the profile is touched"),
    Action("9", "Full pipeline: detach → core → install", "pipeline", danger=True),
    Action("10", "Incompatible list: show contents", "show_incompatible"),
    Action("11", "Settings: profile, target, modes", "settings"),
    Action("12", "CLI flag reference", "help"),
    Action("13", "Inspect: a plugin that is NOT installed yet", "inspect",
           hint="a directory or a .tgz; nothing is installed"),
    Action("14", "Verify: do the installed plugins load — and do they DO anything?", "verify",
           hint="imports each one, calls apply(), calls the route handlers it registered, "
                "reports shadowed surfaces"),
    Action("0", "Exit", "quit"),
)

ACTIONS_BY_KEY = {action.key: action for action in ACTIONS}

#: Menu items that only show something — the profile is never touched.
#: Item 4 (check) belongs here: it reads the profile and writes the state files
#: (the incompatible list, the report, the cache), but it never installs or
#: detaches anything, so it cannot change the profile.
READ_ONLY_KEYS = frozenset({"1", "2", "3", "4", "10", "11", "12", "13", "14", "0"})


@dataclass
class Menu:
    """The interactive menu."""

    settings: Settings = field(default_factory=Settings)
    console: Console = field(default_factory=Console)
    dispatch: dict = field(default_factory=dict)
    running: bool = True

    # ------------------------------------------------------------------ output
    def header(self) -> None:
        settings = self.settings
        core = core_version()
        plugins = "?"
        directory = profile_dir(settings.profile)
        try:
            if (directory / "package.json").is_file():
                plugins = str(len(read_profile(directory).plugins))
        except OSError:
            plugins = "?"
        self.console.write()
        self.console.write(line())
        self.console.write("  " + self.console.paint(
            "dsh-upgrade — DSH core and profile plugin upgrade", "bold", "cyan"))
        self.console.write(
            "  core " + self.console.paint(core or "not found", "bold")
            + " · profile " + self.console.paint(settings.profile, "bold")
            + " · plugins " + self.console.paint(plugins, "bold")
        )
        self.console.write(
            f"  target: {settings.target_label()} · mode: {settings.mode_label()}"
            f" · core directory: {core_install_dir() or '—'}"
        )
        self.console.write(line())

    def render(self) -> None:
        self.header()
        self.console.write("  " + self.console.paint(
            "read-only — the profile is not touched", "dim"))
        for action in ACTIONS:
            if action.key not in READ_ONLY_KEYS:
                continue
            self.console.write(self.action_line(action))
        self.console.write()
        self.console.write("  " + self.console.paint("may change the profile", "yellow"))
        for action in ACTIONS:
            if action.key in READ_ONLY_KEYS:
                continue
            self.console.write(self.action_line(action))
        self.console.write(line())
        self.console.write("  " + self.console.paint(
            "Tip: 4 — check before upgrading, 9 — the whole pipeline.", "dim"))

    def action_line(self, action: Action) -> str:
        """One rendered menu item (key, fixed title column, hint, danger mark)."""
        key = self.console.paint(f"{action.key:>3}", "bold", "cyan")
        mark = self.console.paint(" ! ", "bold", "red") if action.danger else "   "
        title = action.title
        hint = self.console.paint(f"  — {action.hint}", "dim") if action.hint else ""
        return f" {key}{mark}{title}{hint}"

    # --------------------------------------------------------------- arguments
    def args(self, command: str, **overrides) -> Namespace:
        """Build a Namespace with every field the commands read."""
        settings = self.settings
        values = {
            "command": command,
            "profile": settings.profile,
            "core": settings.core,
            "offline": settings.offline,
            "no_clone": settings.no_clone,
            "json": settings.json_output,
            "color": settings.color,
            "verbose": settings.verbose,
            "state_dir": settings.state_dir,
            "checkouts": settings.checkouts,
            # flags of specific commands
            "update": False,
            "detach_first": False,
            "yes": False,
            "dry_run": False,
            "from_file": None,
            "only": None,
            "install_unknown": False,
            "prune_failed": False,
            "file": None,
            "install": False,
            "limit": 6,
            "all": False,
            "run_core_upgrade": False,
            "skip_attach": False,
            "artifact": None,
            "since": None,
            # flags of the runtime verification
            "verify": False,
            "cached": False,
            "loader": False,
        }
        values.update(overrides)
        return Namespace(**values)

    def call(self, command: str, **overrides) -> int:
        """Call the very same function as the CLI and return its code."""
        handler = self.dispatch.get(command)
        if handler is None:
            raise RuntimeError(f"command {command} is not available")
        return handler(self.args(command, **overrides))

    def ask_core(self) -> None:
        """Ask for the target core version (empty keeps the current one)."""
        current = core_version() or "not found"
        self.console.write()
        self.console.write(f"  Current core: {current}; current target: "
                           f"{self.settings.core or 'auto'}")
        self.console.write("  Enter a version (for example 0.1.5-rc.2), "
                           "'-' for auto, Enter to keep it.")
        self.console.write("  'auto' picks the newest published version "
                           f"({self.settings.auto_target(refresh=True)}).")
        answer = self.console.ask("  version: ")
        if answer in ("-", "auto"):
            self.settings.core = None
        elif answer:
            self.settings.core = answer

    def ask_only(self, action: str) -> list[str] | None:
        """Let the reader narrow an operation to some plugins (``--only``).

        Enter keeps the whole profile, which is the default of the operation; names
        are validated by the command itself, so a typo is reported rather than
        silently ignored.
        """
        answer = self.console.ask(
            f"  Only some plugins? Names separated by spaces (Enter — every plugin) "
            f"to {action}: ").strip()
        return answer.split() or None

    # ----------------------------------------------------------------- actions
    def shadows_now(self):
        """``(profile, shadowed plugins)`` for the selected profile, probe-free.

        The menu needs this answer before it can offer the loader tree, and the
        effective tree is read from files alone — no plugin is executed for it.
        """
        directory = profile_dir(self.settings.profile)
        if not (directory / "package.json").is_file():
            return None
        try:
            profile = read_profile(directory)
            effective = effects_mod.resolve(profile)
            return profile, effects_mod.scan_shadows(profile, effective)
        except (OSError, ValueError):
            return None

    def wire_now(self):
        """``(profile, dead RPC calls)`` for the selected profile, probe-free.

        Read from the plugin files against the installed core's own wire
        declarations, so the warning costs nothing and needs no host.
        """
        directory = profile_dir(self.settings.profile)
        if not (directory / "package.json").is_file():
            return None
        try:
            profile = read_profile(directory)
            return profile, wire_mod.scan_profile(profile)
        except (OSError, ValueError):
            return None

    def warn_wire(self) -> None:
        """Name the calls this core does not serve, before the reader acts on them.

        A dead RPC path is the one failure that shows up nowhere else: the entry
        imports, ``apply()`` runs, and the feature is silent. Saying it here keeps
        the reader from diagnosing it in the browser console.
        """
        if self.settings.json_output:
            return
        found = self.wire_now()
        if found is None:
            return
        _, wire = found
        if not wire:
            return
        self.console.write()
        for name, calls in sorted(wire.items()):
            for call in calls:
                self.console.write("  " + self.console.paint(
                    f"! {name}: {call.path} is not an endpoint of this core",
                    "bold", "red"))
                self.console.write("    " + self.console.paint(call.note, "dim"))
        self.console.write("  " + self.console.paint(
            "the calls are read from the code; the full section: "
            + invocation_mod.command("verify", profile=self.settings.profile), "dim"))

    def offer_loader(self) -> None:
        """Show the loader tree here, instead of naming a command to run later.

        Nothing is asked unless a plugin is actually shadowed: the tree matters when
        something loads and then does nothing, which is the one thing the report
        cannot show by itself. Inside the menu there is no reason to leave it, retype
        the command and remember the profile. A reference that an enabled
        replacement already explains does not count — that plugin is drawing into a
        component that is mounted, under a different name.
        """
        if self.settings.json_output:
            return
        found = self.shadows_now()
        if found is None:
            return
        profile, shadows = found
        shadowed = {name: [shadow for shadow in items if not shadow.explained]
                    for name, items in shadows.items()}
        shadowed = {name: items for name, items in shadowed.items() if items}
        if not shadowed:
            return
        self.console.write()
        if not self.console.ask_yes(
                f"  {len(shadowed)} plugin(s) draw into a switched-off component — print "
                "the effective loader tree now?", default=True):
            self.console.write("  " + self.console.paint(
                "the same tree: " + invocation_mod.loader_hint(self.settings.profile), "dim"))
            return
        from dsh_upgrade import print_loader_tree
        print_loader_tree(profile, shadowed)

    def act_status(self) -> None:
        # The overview has to stay cheap, so nothing is executed here: the loads
        # column shows the verdicts a previous verify left in the cache (a stale cache
        # is ignored, an absent one leaves the column at "?"). The runtime probe is
        # item 14; the surface column and the shadowed-surface section come from the
        # effective loader tree, which is read from the profile's own patch layers.
        self.call("status")
        self.warn_wire()
        self.offer_loader()

    def act_verify(self) -> None:
        """Runtime check: import every installed plugin and call its apply()."""
        live = self.console.ask_yes(
            "  Also ask the running DSH which of those surfaces it really serves "
            "(--live)?", default=False)
        code = self.call("verify", live=live)
        if code == 2:
            self.console.write()
            self.console.write("  " + self.console.paint(
                "Some plugins do not import — see the details above.", "yellow"))
        elif code == 0:
            self.console.write()
            self.console.write("  " + self.console.paint(
                "Every installed plugin imported cleanly.", "green"))
        self.console.write()
        self.console.write("  " + self.console.paint(
            "Loading is not working: the 'surface' column says what each plugin actually "
            "registers, a shadowed row means the UI it draws into is switched off, and "
            "'wire:404' means its calls address an endpoint this core does not serve.",
            "dim"))
        self.warn_wire()
        self.offer_loader()

    def act_core_versions(self) -> None:
        self.call("core_versions")

    def act_plan(self) -> None:
        answer = self.console.ask("  How many recent new versions to look at? [6] (all — every one): ")
        if answer.lower() in ("all", "*"):
            self.call("plan", all=True)
        else:
            limit = 6
            if answer.isdigit():
                limit = int(answer)
            self.call("plan", limit=limit)

    def act_check(self) -> None:
        self.ask_core()
        update = self.console.ask_yes("  Also check the newest plugin versions (--update)?",
                                      default=False)
        verbose = self.console.ask_yes("  Full detail for every plugin (--verbose)?",
                                       default=self.settings.verbose)
        code = self.call("check", update=update, verbose=verbose)
        if code == 2:
            self.console.write()
            self.console.write("  " + self.console.paint(
                "There are incompatible plugins — the full breakdown is in the list (item 10).",
                "yellow"))
        elif code == 0:
            self.console.write()
            self.console.write("  " + self.console.paint("No incompatible plugins.", "green"))

    def act_inspect(self) -> None:
        """Check an artifact that is not installed — nothing is written to the profile."""
        artifact = self.console.ask_path(
            "  Path to a plugin directory or a .tgz (Tab completes, ~ works): ")
        if not artifact:
            self.console.write("  cancelled")
            return
        since = self.console.ask("  Baseline core version that shipped the packages it may "
                                 "reference (--since, empty = installed): ").strip()
        code = self.call("inspect", artifact=artifact, since=since or None)
        if code == 2:
            self.console.write()
            self.console.write("  " + self.console.paint(
                "The artifact would not boot on this core — see the factory/registration "
                "detail above.", "yellow"))
        elif code == 0:
            self.console.write()
            self.console.write("  " + self.console.paint("No incompatibility proven.", "green"))

    def act_detach(self) -> None:
        distinct = self.console.ask_yes(
            "  Detach only some plugins (--only)? The snapshot still records every plugin.",
            default=False)
        only = self.ask_only("detach") if distinct else None
        self.console.write()
        self.console.write("  " + self.console.paint("PREVIEW (--dry-run): what will be detached",
                                                     "bold"))
        self.call("detach", dry_run=True, only=only)
        scope = "ALL plugins of the profile" if not only else f"{len(only)} selected plugin(s)"
        if not self.console.confirm_yes(
                f"  Detach {scope}? A snapshot will be written, the data (~/.dsh) stays."):
            self.console.write("  cancelled")
            return
        self.call("detach", yes=True, only=only)

    def act_attach(self) -> None:
        self.ask_core()
        from_file = self.console.ask_path(
            "  Snapshot file (Enter — the freshest one, Tab completes): ")
        distinct = self.console.ask_yes(
            "  Restore only some plugins from the snapshot (--only)?", default=False)
        only = self.ask_only("restore") if distinct else None
        update = self.console.ask_yes("  Install the newest compatible versions (--update)?",
                                      default=True)
        unknown = self.console.ask_yes(
            "  Also install unconfirmed plugins (--install-unknown), with a post-install "
            "code check?",
            default=False)
        prune = self.console.ask_yes("  Detach the plugins that fail the post-check "
                                     "(--prune-failed)?", default=False)
        overrides = {"update": update, "install_unknown": unknown, "prune_failed": prune,
                     "only": only}
        if from_file:
            overrides["from_file"] = from_file
        self.console.write()
        self.console.write("  " + self.console.paint("PREVIEW (--dry-run): what will be installed",
                                                     "bold"))
        self.call("attach", dry_run=True, **overrides)
        if not self.console.confirm_yes("  Install the listed plugins?"):
            self.console.write("  cancelled")
            return
        self.call("attach", yes=True, **overrides)

    def act_recheck(self) -> None:
        self.ask_core()
        path = self.console.ask_path(
            "  List file (Enter — by target core / the freshest one, Tab completes): ")
        install = self.console.ask_yes("  Also install the plugins that became compatible "
                                       "(--install)?", default=True)
        unknown = False
        if install:
            unknown = self.console.ask_yes(
                "  Also install the unconfirmed ones (--install-unknown), with a post-install "
                "code check?", default=False)
        update = self.console.ask_yes("  And install the newest versions right away (--update)?",
                                      default=False)
        overrides = {"install": install, "install_unknown": unknown, "update": update}
        if path:
            overrides["file"] = path
        if install:
            self.console.write()
            self.console.write("  " + self.console.paint("PREVIEW (--dry-run)", "bold"))
            self.call("recheck", dry_run=True, **overrides)
            if not self.console.confirm_yes("  Recheck and install them?"):
                self.console.write("  cancelled")
                return
            self.call("recheck", yes=True, **overrides)
        else:
            self.call("recheck", **overrides)

    def act_plugins(self) -> None:
        unknown = self.console.ask_yes(
            "  Also install a newer version that is not confirmed for this core "
            "(--install-unknown), with a post-install code check?", default=False)
        detach_first = self.console.ask_yes(
            "  Remove each plugin before installing its new version (--detach-first)? "
            "Otherwise the new version is installed over the current copy.",
            default=False)
        overrides = {"install_unknown": unknown, "detach_first": detach_first}
        self.console.write()
        self.console.write("  " + self.console.paint("PREVIEW (--dry-run)", "bold"))
        self.call("plugins", dry_run=True, **overrides)
        if not self.console.confirm_yes(
                "  Update every plugin that has a newer version (the core is left alone)?"):
            self.console.write("  cancelled")
            return
        self.call("plugins", yes=True, **overrides)

    def act_pipeline(self) -> None:
        self.ask_core()
        self.console.write()
        self.console.write(f"  Target core version: {self.settings.target_label()}")
        update = self.console.ask_yes("  Install the newest compatible versions (--update)?",
                                      default=True)
        unknown = self.console.ask_yes(
            "  Also install the unconfirmed plugins (--install-unknown), judging them by the "
            "post-install code check?", default=False)
        run_core = self.console.ask_yes(
            "  Run the core upgrade itself (npm i -g, needs access outside the workspace)?",
            default=False)
        skip_attach = self.console.ask_yes(
            "  Stop after detaching the plugins (--skip-attach)?", default=False)
        self.console.write()
        self.console.write("  " + self.console.paint("PREVIEW (--dry-run)", "bold"))
        self.call("pipeline", dry_run=True, update=update, install_unknown=unknown,
                  run_core_upgrade=run_core, skip_attach=skip_attach)
        if not self.console.confirm_yes(
                "  Run the pipeline: snapshot → detach → "
                + ("core upgrade → " if run_core else "")
                + "install "
                + ("the compatible and unconfirmed plugins?" if unknown
                   else "the compatible plugins?")):
            self.console.write("  cancelled")
            return
        self.call("pipeline", yes=True, update=update, install_unknown=unknown,
                  run_core_upgrade=run_core, skip_attach=skip_attach)

    def act_show_incompatible(self) -> None:
        target = self.settings.core or self.settings.auto_target()
        path = None
        if target:
            candidate, _ = snapshot.incompatible_path(target)
            if candidate.is_file():
                path = candidate
        if path is None:
            path = snapshot.latest_incompatible()
        if path is None:
            self.console.write()
            self.console.write("  No list yet — run a check first (item 4).")
            return
        try:
            entries = snapshot.load_incompatible(path)
        except (OSError, ValueError) as error:
            self.console.write()
            self.console.write(f"  could not read {path}: {error}")
            return
        self.console.write()
        self.console.write(f"  File: {path}")
        if not entries:
            self.console.write("  The list is empty — every plugin is compatible.")
            return

        by_status: dict[str, list] = {}
        for entry in entries:
            by_status.setdefault(entry.get("status"), []).append(entry)
        groups = []
        for status in ("incompatible", "unknown", "compatible"):
            items = by_status.get(status) or []
            if not items:
                continue
            label = f"{GLYPH.get(status, '??')} — {len(items)}"
            payload = self.console.paint(label, "bold", "red" if status == "incompatible"
                                         else ("yellow" if status == "unknown" else "green"))
            groups.append((payload, [[
                self.console.paint(GLYPH.get(entry.get("status"), "??"), "bold"),
                entry.get("name") or "—",
                entry.get("version") or "—",
                entry.get("sourceLabel") or entry.get("source") or "—",
                entry.get("requirement") or "—",
                entry.get("reason") or "",
            ] for entry in items]))
        self.console.write()
        self.console.write(grouped_table(["", "plugin", "version", "source", "requirement",
                                          "reason"], groups))
        self.console.write()
        self.console.write(f"  Human readable version: {Path(path).with_suffix('.md')}")

    def _persist(self) -> None:
        """Write the settings file after a change — settings outlive the menu."""
        saved = self.settings.save()
        if saved is None:
            self.console.write(self.console.paint(
                "  the settings file could not be written — this change is for this run only",
                "yellow"))
            return
        self.console.write(self.console.paint(f"  saved: {saved}", "dim"))

    def act_settings(self) -> None:
        settings = self.settings
        while True:
            self.console.write()
            self.console.write("  Settings (they apply to every menu item):")
            self.console.write(f"   1 profile:           {settings.profile}")
            self.console.write(f"   2 target core:       {settings.target_label()}")
            self.console.write(f"   3 offline:           {'yes' if settings.offline else 'no'}"
                               "  (cache only, no network)")
            self.console.write(f"   4 no clone:          {'yes' if settings.no_clone else 'no'}"
                               "  (do not clone the checkout)")
            self.console.write(f"   5 verbose output:    {'yes' if settings.verbose else 'no'}")
            self.console.write(f"   6 color:             {settings.color}")
            self.console.write(f"   7 state directory:   "
                               f"{settings.state_dir or 'default (state/ of the repository)'}")
            self.console.write(f"   8 checkouts:         {settings.checkouts_label()}")
            self.console.write("   9 remove the temporary checkouts now")
            self.console.write(f"  10 forget saved settings ({config_mod.config_path()})")
            self.console.write("   0 back")
            self.console.write(self.console.paint(
                "  Every change is written to the settings file and applies to later runs; "
                "a command-line flag still wins for the run it was given on.", "dim"))
            choice = self.console.ask("  choice: ")
            if choice in ("", "0", "q"):
                return
            if choice == "1":
                answer = self.console.ask(f"  profile [{settings.profile}]: ")
                if answer:
                    settings.profile = answer
                    self._persist()
            elif choice == "2":
                self.ask_core()
                self._persist()
            elif choice == "3":
                settings.offline = not settings.offline
                self._persist()
            elif choice == "4":
                settings.no_clone = not settings.no_clone
                self._persist()
            elif choice == "5":
                settings.verbose = not settings.verbose
                self._persist()
            elif choice == "6":
                answer = self.console.ask("  color (auto/always/never) [auto]: ").lower()
                settings.color = style.normalize_mode(answer)
                style.set_mode(settings.color)
                self._persist()
            elif choice == "7":
                answer = self.console.ask_path("  state directory (Enter — default): ")
                settings.state_dir = answer or None
                if settings.state_dir:
                    import os
                    os.environ["DSH_UPGRADE_STATE"] = settings.state_dir
                self._persist()
            elif choice == "8":
                self.console.write(self.console.paint(
                    "     'temp' — under the system temp directory, reused between runs, "
                    "cleared on reboot (default)", "dim"))
                self.console.write(self.console.paint(
                    f"     'keep' — {paths.dsh_home() / 'checkouts'}", "dim"))
                self.console.write(self.console.paint(
                    "     or a directory to keep the checkouts in (Tab completes, ~ works)", "dim"))
                answer = self.console.ask_path("  new location (Enter — keep the current one): ")
                if answer:
                    settings.checkouts = paths.normalize_checkouts(answer)
                    paths.set_checkouts_setting(settings.checkouts)
                    self._persist()
            elif choice == "9":
                removed = paths.prune_checkouts()
                if removed:
                    for path in removed:
                        self.console.write(f"  removed {path}")
                else:
                    self.console.write(f"  nothing to remove ({paths.temp_checkouts_root()} "
                                       "does not exist)")
            elif choice == "10":
                removed = config_mod.forget()
                if removed is None:
                    self.console.write("  nothing was saved")
                else:
                    self.console.write(f"  removed {removed}")
                settings.apply_config(config_mod.DEFAULTS)
                paths.set_checkouts_setting(settings.checkouts)
                style.set_mode(settings.color)
            else:
                self.console.write("  no such item")

    def act_help(self) -> None:
        from dsh_upgrade import build_parser
        self.console.write()
        build_parser().print_help(file=self.console.stdout)
        self.console.write("\n  Any of these commands can be called directly, without the menu.")

    def act_quit(self) -> None:
        self.running = False

    # -------------------------------------------------------------------- loop
    def run(self) -> int:
        while self.running:
            self.render()
            try:
                choice = self.console.ask("  choice: ").strip().lower()
            except MenuExit:
                self.console.write("\n  exit")
                return 0
            if choice in ("", "0", "q", "exit", "quit"):
                self.console.write("  exit")
                return 0
            action = ACTIONS_BY_KEY.get(choice)
            if action is None:
                self.console.write("  no such item")
                continue
            handler = getattr(self, f"act_{action.handler}")
            try:
                handler()
            except MenuExit:
                self.console.write("\n  exit")
                return 0
            except KeyboardInterrupt:
                self.console.write("\n  interrupted (Ctrl+C). Nothing was changed "
                                   "by this operation.")
            except RuntimeError as error:
                self.console.write(f"  error: {error}")
            except SystemExit as error:  # commands use SystemExit to refuse
                self.console.write(f"  refused: {error}")
            try:
                self.console.pause()
            except MenuExit:
                self.console.write("\n  exit")
                return 0
        return 0


def default_dispatch() -> dict:
    """The real CLI commands (imported inside the function — no circular imports)."""
    import dsh_upgrade

    return {
        "status": dsh_upgrade.cmd_status,
        "core_versions": dsh_upgrade.cmd_core_versions,
        "verify": dsh_upgrade.cmd_verify,
        "plan": dsh_upgrade.cmd_plan,
        "check": dsh_upgrade.cmd_check,
        "inspect": dsh_upgrade.cmd_inspect,
        "detach": dsh_upgrade.cmd_detach,
        "attach": dsh_upgrade.cmd_attach,
        "recheck": dsh_upgrade.cmd_recheck,
        "plugins": dsh_upgrade.cmd_plugins,
        "pipeline": dsh_upgrade.cmd_pipeline,
    }


def menu_map() -> str:
    """The menu items as text — for a non-interactive run."""
    lines = ["dsh-upgrade — available actions:"]
    for action in ACTIONS:
        mark = " ! " if action.danger else "   "
        lines.append(f"  {action.key:>3}{mark}{action.title}")
    lines.append("")
    lines.append("Run it without arguments in a terminal to open this menu.")
    lines.append("Or call the commands directly: python3 dsh_upgrade.py <command> --help")
    return "\n".join(lines)


def run(settings: Settings | None = None, console: Console | None = None,
        dispatch: dict | None = None) -> int:
    """Open the menu. Without a TTY it prints the action map and exits."""
    menu = Menu(
        settings=settings or Settings(),
        console=console or Console(),
        dispatch=dispatch if dispatch is not None else default_dispatch(),
    )
    style.set_mode(menu.settings.color)
    paths.set_checkouts_setting(menu.settings.checkouts)
    if dispatch is None:
        stream = menu.console.stdin
        if not getattr(stream, "isatty", lambda: False)():
            menu.console.write(menu_map())
            return 0
    try:
        return menu.run()
    except KeyboardInterrupt:
        menu.console.write("\n  exit")
        return 0
