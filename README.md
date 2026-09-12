# dsh-upgrade-tools

Tools for upgrading the DeepSeek Harness core and the plugins of a profile.

[Repository](https://github.com/ThinkForge-core/dsh-upgrade-tools) · MIT ·
runs against **the DSH core you have installed** — no core version is pinned
(see [Compatibility](#compatibility)).

Runs as **plain Python 3** (standard library only, no npm and no Node). The read-only actions
(`status`, `core-versions`, `plan`, `check`, `inspect`) work against a **running** harness too —
they only read the profile, the installed core and the registry. Only a clean upgrade
(`detach` → core/plugin install → `attach`) needs DSH **shut down**, because the profile
directory is being rewritten.
Requirements: Python 3 and the `dsh` CLI on `PATH` for the actions that read or change the
profile (or `DSH_INSTALL_DIR` pointing at the installed core).
The script pulls in everything it needs for its checks itself: the npm registry over HTTP, the
marketplace index, and — for breaking-change analysis — a checkout of the core version
(`git clone --depth 1` of the `dsh-v<version>` tag: one commit, because the comparison reads files
and never walks history). By default that checkout goes under the **system temporary directory**
(`/tmp`): the next run reuses it instead of downloading the same tag again, and the operating system
clears it on reboot. A directory set in the settings (or by `--checkouts`) is used instead.

The main idea is "detach everything, upgrade the core, install it all back, and set the
incompatible ones aside into a list":

```
check ──► snapshot ──► detach all plugins ──► core upgrade ──► install compatible ones
                                                             └─► the rest into the list
```

## Compatibility

**No core version is pinned.** Every command reads the core that is actually installed — found
through the `dsh` CLI on `PATH`, or named by `DSH_INSTALL_DIR` — and takes its facts from that core's
own bytes: the package manifests, the generated TYPERT faces that declare its wire endpoints, its
effective loader tree. Nothing assumes a release, so the installed core is always the baseline and
upgrading *from* an old one is the ordinary case rather than a special mode.

Verified end-to-end against three releases of the `0.1.x` line:

| Installed core | Host packages | Client rows | TYPERT faces | Wire endpoints | `status` | `check` |
|---|---|---|---|---|---|---|
| `0.1.0-rc.8` | 193 | 43 | 7 | 26 | exit 0 | exit 0 |
| `0.1.1-rc.2` | 194 | 43 | 7 | 26 | exit 0 | exit 0 |
| `0.1.5-rc.2` | 237 | 55 | 15 | 84 | exit 0 | exit 0 |

Those numbers differ per release because they are *read* from that release, not assumed. A core whose
surfaces the tool cannot interpret is reported as such — an empty inventory, a wire check that says it
was not performed — rather than guessed at.

The installed core is read from either layout the harness can produce: a **global install**, whose
packages are nested under `<core>/node_modules/@deepseek-ai`, or a **hoisted tree**, where they sit
beside the core package. Reading only one of the two silently loses part of the inventory — and the
verdicts that depend on it.

One version-dependent input is worth knowing about: when no checkout of the target core is available,
the inline-purity rule (check 6) falls back to a built-in classification copied from `0.1.5-rc.2`.
That set is deliberately broad, so a missing entry relaxes the check instead of inventing an
incompatibility; naming the target with `--core` and letting the tool fetch its checkout replaces the
built-in set with the target's own rule.

## Quick start

**With no arguments the interactive menu opens** — every action is reachable by clicking its
number, and destructive ones are always shown in preview mode first:

```bash
git clone https://github.com/ThinkForge-core/dsh-upgrade-tools.git
cd dsh-upgrade-tools
python3 dsh_upgrade.py          # menu: status, plan, check, detach, install, pipeline…
python3 scripts/menu.py         # the same thing
```

The same tool also works as an ordinary CLI (scripts, cron):

```bash
# 1. Current state: core version, tags, installed plugins
python3 dsh_upgrade.py status

# 2. Which core versions exist at all and what happens to plugins on each
python3 dsh_upgrade.py plan

# 3. Full check of the chosen version (with a checkout and code scans)
python3 dsh_upgrade.py check --core 0.1.5-rc.2 --verbose

# 4. Full pipeline: snapshot → detach → (core upgrade) → install
python3 dsh_upgrade.py pipeline --core 0.1.5-rc.2 --yes

# 5. Or plugins only, leaving the core alone
python3 dsh_upgrade.py plugins --update --yes

# 6. Later: recheck the deferred ones and install the ones that became compatible
python3 dsh_upgrade.py recheck --install --yes

# 7. After an upgrade: do the installed copies actually load — and run?
python3 dsh_upgrade.py verify
```

## Menu (run without arguments)

```
dsh-upgrade — DSH core and profile plugin upgrade
  core 0.1.1-rc.2 · profile web · plugins 16
  target: auto — 0.1.5-rc.2 · mode: online · core directory: …/node_modules/@deepseek-ai/dsh

  read-only — the profile is not touched
   1   Status: core, tags, profile plugins
   2   Core versions: what the registry offers
   3   Plan: new core versions and what happens to plugins
   4   Check: full compatibility matrix
  10   Incompatible list: show contents
  11   Settings: profile, target, modes
  12   CLI flag reference
  13   Inspect: a plugin that is NOT installed yet
   0   Exit

  may change the profile
   5 ! Snapshot and detach ALL plugins
   6 ! Install plugins from a snapshot
   7   Recheck the incompatible list
   8 ! Update plugins only (leave the core alone)
   9 ! Full pipeline: detach → core → install

Tip: 4 — check before upgrading, 9 — the whole pipeline.
```

* The header always shows the context: core version, profile, plugin count, target version, mode.
* The items are **grouped by risk**: "read-only — the profile is not touched" first, then
  everything that may change the profile. The `!` mark and the red key flag the destructive ones.
  Item **4** (check) is read-only: it reads the profile and writes the state files (the
  incompatible list, the report, the cache), but it installs and detaches nothing.
* Item **11** changes the parameters for every action: profile, target core (or "auto"),
  `offline`, "no clone", verbose output, colors, state directory and where the version
  checkouts live. **Every change is written to the settings file** and applies to later runs
  (see below).
* **Path prompts are terminal-grade.** Item **13** (inspect), the snapshot file of item 6 and the
  list file of item 7 read a path the way a shell does: Tab completes files and directories
  (directories get a trailing slash, spaces are escaped, `file:`/`link:` specifiers are completed
  after the prefix), arrows/Home/End edit the line, up-arrow recalls earlier paths. Quotes,
  backslash escapes and `~` in what you typed are resolved before the path is used. Outside a
  terminal the ordinary reader is used, so nothing changes for scripts.
* The automatic target is written as `auto — <version>` and is the **newest published** release
  (prereleases included) — not the installed one. The installed core shows up in that line only
  as a marked fallback (`auto — 0.1.1-rc.2 (installed fallback)`) when the registry cannot be
  reached at all. The header resolves it from the registry cache, so it never blocks.
* Items 5, 6, 8, 9 **are first executed with `--dry-run`** and ask for confirmation with the word
  `yes`; the core upgrade (`npm i -g`) in the pipeline is only printed by default.
* The menu does not duplicate logic: it assembles the same arguments and calls the same `cmd_*`
  functions as the CLI.
* Item **14** (verify) executes the question the declarations cannot answer: it imports the
  installed copy of every plugin, calls its `apply()`, and reports what each one registers — plus any
  plugin whose UI host row is switched off (see
  [Do they actually work?](#do-they-actually-work)). It also asks whether to run the `--live` probe
  against the running DSH. Item **1** (status) shows both columns and the shadowed-surface section,
  and fills the probe results in by itself when the cache is missing or stale.
* Run without arguments **outside a terminal** (a pipe, cron, a script) it does not hang — it
  prints the action map (`dsh-upgrade — available actions:`) and exits with code 0.

### Settings are remembered

Options are kept in a small JSON file, so a choice made once does not have to be retyped as flags
on every run:

| | |
|---|---|
| Location | `$DSH_UPGRADE_CONFIG`, else `$XDG_CONFIG_HOME/dsh-upgrade/config.json`, else `~/.config/dsh-upgrade/config.json` |
| Written by | the settings screen (menu item 11) — nothing else: a check, a plan or an upgrade never writes it |
| Priority | an explicit flag (that run only) > the environment > the saved file > the built-in default |

A flag given on the command line is **never** written back, so `dsh_upgrade.py --profile other`
stays a one-off. Item 11 → 10 ("forget saved settings") deletes the file and returns to the defaults;
item 11 → 9 removes the temporary checkouts right now.

**Where the version checkouts live** is one of those saved options:

| Value | Meaning |
|---|---|
| `temp` (default) | `<tmp>/dsh-upgrade-checkouts-<uid>` — under the system temporary directory, so the OS clears it on reboot; reused between runs, and removable now with `--prune-checkouts` (menu item 11 → 9) |
| `keep` | `<DSH_HOME>/checkouts`, kept between runs |
| a directory | anything you point at: `--checkouts DIR`, or menu item 11 → 8 |

A checkout is a means to an end (the comparison), so the default does not accumulate in a permanent
place. Whatever the setting, an existing checkout is reused — the configured location first, then
`<DSH_HOME>/checkouts` — and only cloned (with `--depth 1`) when neither has it. `DSH_CHECKOUTS_ROOT`
beats the saved setting (see [Environment variables](#environment-variables)).

## Commands

| Command | What it does |
|---|---|
| `menu` | Interactive menu (the same one opens with no arguments). |
| `status [--verify] [--loader] [--summary]` | Core version and directory, tags, recent versions, table of plugins with sources **and `loads` + `surface` columns** — whether the installed copy imports, and what it actually registers. `--verify` fills them in (probes only when what it measured has changed); `--loader` prints the whole effective loader tree, marking the rows a shadowed plugin draws into; `--summary` prints one line of counts instead of the report. |
| `verify [--cached] [--live] [--no-handlers] [--web-url URL] [--loader] [--summary \| --diff PATH]` | Import the installed copy of every plugin with `node`, call its `apply()`, then **call every route handler `apply()` registered once** and read what it threw and logged — plus which plugins have a **shadowed surface** (their UI host row is switched off). `--live` also GETs every registered route on the running DSH (`--web-url`, default `http://127.0.0.1:3080`), which proves the row applied. `--no-handlers` skips the handler calls — a weaker answer, so it is not cached. `--loader` prints the effective loader tree with the shadowed rows marked. `--summary` prints one line of counts; `--diff PATH` prints only what changed since a result saved earlier. Writes `state/verified-<profile>.json`. |
| `core-versions` | All core versions and tags, how many are newer than the installed one. |
| `plan [--limit N] [--all]` | **In a single run**: all new core versions and what happens to plugins on each (fast, declarations only). |
| `check --core V [--update] [--summary]` | Compatibility matrix: what happens to each plugin on version `V`. Writes the incompatible list; `--summary` prints one line of counts instead of the matrix. |
| `inspect PATH [--core V] [--since OLD]` | Compatibility of an artifact that is **not installed yet** — a plugin directory or a `.tgz`. Reads only that artifact; the profile is not read and nothing is installed. |
| `detach [--yes]` | Snapshot of the profile and detaching of ALL plugins. Without `--yes` it only writes the snapshot and aborts. |
| `attach [--from F] [--update] [--install-unknown] [--prune-failed] [--yes]` | Installation from a snapshot: installs the compatible ones, the rest go into the list. |
| `recheck [--file F] [--install] [--install-unknown] [--yes]` | Recheck the incompatible list and install the ones that became compatible; with `--install-unknown` also the unconfirmed ones (post-checked). |
| `plugins [--update] [--yes]` | Update plugins only, on the current core. |
| `pipeline [--core V] [--update] [--install-unknown] [--run-core-upgrade] [--yes]` | Full pipeline from check to post-check. `--install-unknown` is **off** by default: only proven-compatible plugins are installed, everything else goes into the incompatible list. |

Wrapper scripts for each step live in `scripts/` (`menu.py`, `status.py`, `verify.py`, `plan.py`,
`check.py`, `artifact.py` for `inspect`, `detach.py`, `attach.py`, `recheck.py`, `pipeline.py`,
`plugins.py`, `cores.py`); arguments are passed through. The wrapper for `inspect` is called
`artifact.py` because a file named `inspect.py` would shadow the stdlib `inspect` module for every
import in the tool.

Common flags: `--profile` (default `web`), `--core`, `--offline`, `--no-clone`,
`--json`, `--verbose`, `--color auto|always|never`, `--state-dir`,
`--checkouts temp|keep|DIR`, `--prune-checkouts` (delete the temporary checkouts and continue — or
exit, when no command was given). They can be given both before
and after the subcommand — the defaults are applied after parsing, because argparse would
otherwise let a subparser's defaults overwrite a flag given before the subcommand.

**The automatic upgrade target is the newest published core version.** Everywhere `--core` is
optional (`check`, `attach`, `recheck`, `pipeline`), the tool resolves the highest published
version by semver, prereleases included, and prints it as
`auto target: 0.1.5-rc.2 — the newest published version (use --core to pick another one)`.
The `latest` dist-tag is deliberately not used: on the real registry `latest` pointed at
`0.1.5-rc.1` while `0.1.5-rc.2` was already published under `next`. The installed core is only a
fallback for when the registry cannot be reached at all; `plan` lists every candidate.

**Unconfirmed plugins are opt-in.** A plugin with no declarations is neither proven compatible
nor proven broken, so `pipeline`, `attach` and `recheck` install it only with
`--install-unknown` (default: off). In the menu the same choice is a question with the `[y/N]`
default. Whatever is installed that way is re-judged afterwards by the post-check over the code
that actually landed, and the failures go into the incompatible list.

## Reports for scripts and agents

`status`, `check`, `verify` and `inspect` accept `--json`. Stdout then carries exactly one JSON
document and the human report and progress lines go to stderr, so the document can be piped into a
parser without filtering. `check --json` still writes `state/check-<core>.json`, and the paths it
wrote are listed in the document under `state`. `status`, `check` and `verify` also accept
`--summary`.

**Reason codes.** Every plugin entry of `check` and `inspect` carries a `reason_code` beside its
`reason` text, so a consumer can classify a finding without parsing prose. A plugin that fails
several checks at once carries the code of the most fundamental one; the detailed hits stay in the
entry's own lists.

| code | finding |
|---|---|
| `PEER_RANGE_MISMATCH` | a `peerDependencies` range does not admit the target core |
| `ENGINES_DSH_MISMATCH` | `engines.dsh` does not admit the target core |
| `REMOVED_PACKAGE_REQUIRED` | the code requires a package the target core no longer ships |
| `BROWSER_MODULE_TABLE_MISS` | a client bundle requires a name missing from the target module table |
| `DUPLICATE_FACTORY_REGISTRATION` | a client bundle registers a factory id the host already owns |
| `DECLARATION_INTEGRITY_FAILURE` | the `dsh.client` declaration, or the bundle it promises, is not loadable on any core |
| `INLINE_PURITY_VIOLATION` | a client bundle inlines a package that must come from the module table |
| `WIRE_ENDPOINT_DEAD` | a literal `/api` call names an endpoint the target core does not serve |
| `WIRE_METHOD_MISMATCH` | the endpoint is served, but the envelope's own method disagrees with it |
| `HANDLER_REFERENCE_ERROR` | a registered route handler threw a `ReferenceError` on its first call |
| `UNKNOWN_DECLARATIONS` | the manifest declares no DSH version — nothing to compare against |

The field is `null` when no check produced a finding with an identifier: a compatible plugin, or a
manifest that could not be read at all.

**Blocking the core upgrade.** Every plugin entry of `check --json` carries `blocks_core_upgrade`,
the answer to "if the pipeline runs, will it stop?". The pipeline holds an incompatible plugin back
and installs the rest, so the field is `false` for a plugin-level incompatibility however the
declarations read. It is `true` only when the target core itself is out of reach — a version older
than the installed one, which an upgrade cannot move down to.

**Verdict or lead.** In `verify --json` the `findings` list collects the runtime findings from all
three sources — broken route handlers, shadowed surfaces and calls the core does not serve — and
gives each a `confidence`:

```json
{
  "plugin": "example-plugin",
  "surface": "handler!",
  "confidence": "verdict",
  "detail": "the first call threw: ReferenceError: SOME_CACHE is not defined",
  "path": "/api/example",
  "reason_code": "HANDLER_REFERENCE_ERROR"
}
```

`"verdict"` is a proof — the probe saw a failure the recording stub cannot have caused, or the
core's own declarations do not contain the call. `"lead"` is evidence a reader still has to
confirm. The per-plugin structures (`plugins`, `shadowed`, `wire`) stay in the document unchanged,
and the shadow and wire entries carry the same `confidence` field.

**One line of counts.** `--summary` replaces the report with a single line:

```
5 plugins checked, 2 incompatible, 1 unknown, 1 wire-dead, 0 handler-failures
```

With `--json` the same numbers are one object — `total`, `incompatible`, `unknown`, `wire_dead`,
`handler_failures` and `exit_code` (the code the command returns). `incompatible` counts the
plugins with a definite negative status, `unknown` the ones no verdict was produced for, and
`wire_dead` and `handler_failures` the plugins with a dead call and with a route handler that fails
on its first call.

**Comparing two runs.** `verify --diff PATH` reads a result saved earlier (`state/verified-<profile>.json`,
or any file with the same shape) and prints only what differs:

```
=== Changes since 2026-09-12 ===
  example-plugin
    loads:   no → yes  (fixed)
    surface: routes:3 → apply!
  gone-plugin: removed
```

The comparison covers the load verdict and the surface of every plugin, and reports a plugin present
on one side only as added or removed. With `--json` it is
`{"changed": [{"plugin": …, "fields": {"loads": {"old": …, "new": …}}}], "added": […],
"removed": […], "unchanged_count": N, "since": "…"}`. The exit code is the one `verify` returns:
`2` when a server entry does not load.

`plan` and `core-versions` print their tables only; they take no `--json`.

## Output: colors, groups, wrapping

* **Colors.** On a terminal the statuses are colored (`ok` green, `NO` red, `??` yellow), headings
  are cyan, paths and commands are highlighted, notes are dim. Piped output stays plain, so logs
  and `--json` are unaffected. The mode is chosen by `--color`, the `DSH_UPGRADE_COLOR` environment
  variable or `NO_COLOR`.
* **Groups.** Every matrix is grouped instead of being one flat dump: the compatibility table by
  status (incompatible → unconfirmed → compatible), `status` by plugin source (npm / local / git),
  `plan` by outcome (safe → unconfirmed present → incompatible present), and the incompatible-list
  viewer by status.
* **Long cells are wrapped, never truncated.** The `reason` column is never cut: the column widths
  are computed from the content, squeezed proportionally to the terminal width, and anything that
  does not fit continues on the next line — aligned under its own column. A full reason is always
  visible, at any width.

## How compatibility is decided

Six independent checks — each catches its own class of breakage. Check 1 reproduces the
marketplace verdict by design (see [Attribution](#attribution)); checks 2–6 go past it:

1. **Declarations of DSH version requirements.** Peer ranges for host packages and
   `engines.dsh` are read — **in both places**: the top-level `engines.dsh` (the marketplace
   engine sees it) and the nested `dsh.engines.dsh` (nobody sees it: neither the core nor the
   marketplace — which is why a plugin that declares it only there can look "not declared" in the
   marketplace). The direction of the failure follows the marketplace policy: `below-min`,
   `exact-pin`, `above-explicit-max` — a definite incompatibility; `above-implicit-ceiling`
   (ranges of the form `^0.0.1`) — not.
2. **Removed packages.** The inventory of the baseline core is compared with the inventory of the
   target version, after which the plugin code is searched for **hard** `require`/`import` of those
   names. The `dsh.client.inject` field is ignored here: it is informational — only a real
   `require` in the built bundle causes a crash. The baseline is the installed core;
   `inspect --since <version>` overrides it, because against the installed core the comparison is
   self-referential and can prove nothing. The TARGET side is deliberately wider than the
   marketplace's host policy: the installed tree unioned with the target checkout inventory. The
   installed tree alone is not the whole target — a platform module bundled into the shell
   (`dsh-client-ui-primitives`) has no `node_modules` entry of its own, and an installed-only
   contrast called it "removed" and rejected a bundle that merely requires a seed word.
3. **Browser module table.** `require` calls in the client bundle are checked against the seed
   words of the target version and its inventory. A miss means a throw of
   `client-modules: require("…") missed the module table` during bundle materialization.
4. **Factory registrations inside the client bundle.** Every browser bundle is loaded as ONE
   graph row and must register exactly one factory — its own. A bundle that also registers a
   factory for another package (a self-registering client bundle was inlined instead of being
   required from the host) makes the loader throw
   `client-modules: duplicate factory registration for "<id>" (bundle executed twice without
   invalidate?)` and DSH does not start. Declarations are satisfied, every package exists, the
   types check out — and still nothing boots, which is exactly why this check exists.
5. **Declaration integrity.** The host composes the boot graph from `dsh.client` and then fetches
   `exports["./client"]`, so the declaration itself is a contract. Checked: `dsh.client` is an
   object, `platform` is a string, `inject`/`external` are string arrays, `immediately` is a
   boolean, a declared client actually exports `./client`, that export is a string or
   `{ default: string }`, the row does not request its own package, and the promised bundle is
   present in a packed artifact. All of it is version-independent: it fails on ANY core.
6. **Inline purity.** A client bundle may inline only wire layers with no shared runtime identity.
   Any `@deepseek-ai/*` package it inlines must be either a module-table row, its own declared
   `dsh.client.external`, an inline-safe layer, a vendored library, or a generated `/remote`
   contribution — the core's own build-time purity rule, read from the target checkout
   (`checkout.inline_classification`). This is the check for a STALE artifact: built under older
   rules, it inlines a module the host now loads as its own row, and the second copy shares no
   state — nothing throws, the duplicate simply stops matching by
   `Symbol`/`instanceof`/singleton and a panel stays empty. The rule is applied to the inlined
   module's actual sub-path, never to the package as a whole.

> **How checks 4–6 are scoped.** All three read only the DECLARED client bundle
> (`exports["./client"]`) — that is the file the loader executes. Scanning every file of the
> package would flag documentation and build scripts that merely quote the facade call. Check 4
> catches a bundle that registers a factory for a row it does not own; check 5 is the
> version-independent half (a declaration the host rejects fails on any core); check 6 is the
> target-dependent half (an artifact built under rules other than the target's).

**Separately — where the artifact comes from.** For plugins with a local source
(`file:`/`link:`/`workspace:`/`portal:`) the manifest and the code are read from the ARTIFACT
ITSELF — the tarball (stdlib `tarfile`, without unpacking) or the directory; the tool does not go
to the registry for such plugins at all. This is fundamental: the same name in npm may hold a
DIFFERENT product — a locally built version and the published one can carry different code and
different peer pins — and a verdict on it would be a verdict about someone else's code. A scan of
a repository directory does not descend into `node_modules`, `.git`, `tests/` and `examples/` —
so that test fixtures do not produce false "NO"s.

Plus two caveats that matter in practice:

* **`unknown` is not "compatible"** — and it is not "broken" either. `??` means the manifest declares
  **nothing about DSH versions**, so there is nothing to compare: the reason line says exactly that
  ("the manifest declares no DSH version, so there is nothing to compare; the code checks are
  clean"). It is a gap in the declarations, not a failure. Such plugins can be installed with
  `--install-unknown` — the post-check after installation re-runs the checks against the actual code
  and filters out the bad ones. To find out whether they actually WORK, ask the runtime instead of
  the declarations: see [Do they actually work?](#do-they-actually-work).
* **Empirical verdict.** If the check runs against the same core on which the plugin is already
  installed, and the code is clean, the plugin counts as good, even if the declarations require a
  newer version. Thus a plugin may declare `^0.1.2-rc.1` (a newer core than the one installed)
  yet work perfectly on `0.1.1-rc.2`; breaking a working installation because of a strict
  declaration is not acceptable.

## Do they actually work?

Declarations say "may run"; they cannot say "does run". **Neither can an import.** A plugin can import
perfectly and still have no effect at all, in four ways that no declaration scan and no import probe can
see:

* **The row never applies.** A cordis plugin whose `inject` names a service the deployment does not
  provide is never applied — the loader leaves its fiber `pending` and `apply()` is simply not called.
* **The surface it draws into is gone.** A client half that augments another component's DOM (or fills
  its slot) keeps working only while that component is mounted. When the effective profile disables or
  replaces that row, the client half loads and then does nothing, in silence.
* **Its calls address an endpoint the core no longer serves.** A client half that POSTs an RPC path the
  gateway has since renamed keeps valid-looking code, imports cleanly and runs `apply()` — and the call
  answers `404`, so the feature is silently dead: every request fails while nothing in the file looks
  wrong.
* **The route is registered, but the handler behind it is broken.** `apply()` puts a *closure* into the
  deployment; it does not run it. A variable deleted in a refactor (`SOME_CACHE is not defined`), a
  renamed service method, a wrong path inside the handler — none of it exists until a request arrives.
  When the handler catches its own error, logs it and answers `200 []`, the surface shows an empty list
  instead of a failure, and *nothing outside catches it*.

The tool therefore answers five separate questions, not one:

| Column | Question | How |
|---|---|---|
| `loads` | did the code import? | `verify` (menu item 14): a `node` process per plugin, cwd = the profile |
| `surface` | what does it put into the deployment? | `verify` calls `apply()` against a recording context; `--live` confirms the routes on the running DSH |
| `surface` (handler) | does it work when it is called? | `verify` calls every registered route handler once, with a synthetic `GET`, and reads what it threw and logged |
| *shadowed* | is the host component it draws into switched off? | the effective loader tree, read from the bundle patches and the profile's own `cordis.patch.yml` |
| *wire* | does the core still serve the calls it makes? | the endpoint set from the installed core's generated TYPERT faces, the calls from the plugin's own files |

**1. The runtime probe — `verify` (menu item 14).** Every installed plugin's mounted entry is imported
by a separate `node` process whose working directory is the profile directory, so the module graph
resolves exactly as it does at DSH boot. This is what catches the failures the declarations miss: a link
to a `@deepseek-ai/<pkg>` the core no longer ships, a peer dependency that cannot be resolved, an
`exports` map that stopped resolving, a syntax error in the entry file.

It then **calls `apply()`** against a recording context: every service the plugin reaches for is a proxy
that records the method name, `ctx.effect(cb)` and `ctx.inject(deps, cb)` callbacks are invoked (that is
where many plugins do their registration), and every `webServer.register({path, handler})` is harvested
as a route. A plugin can mount *several* modules — a package, one of its subpaths and another subpath —
so every mounted specifier is probed and folded into one verdict; probing only the bare name would call
a working plugin a no-op.

Finally it **calls the handlers** (§ "Handler calls" below) — the step that catches a *partial* failure.

```bash
python3 dsh_upgrade.py verify          # import + apply() + call the route handlers
                                       # writes state/verified-<profile>.json
python3 dsh_upgrade.py verify --cached # reuse the cached verdicts
python3 dsh_upgrade.py verify --live   # also GET every registered route on the running DSH
python3 dsh_upgrade.py verify --no-handlers  # skip the handler calls (weaker; not cached)
python3 dsh_upgrade.py status --verify # the same verdicts as the `loads` column of status
python3 dsh_upgrade.py status --loader # the whole effective loader tree, shadowed rows marked
```

`--live` is the empirical one, and it is opt-in because it really does reach the plugin's handlers: a
`GET` to a registered path answers anything except `404`, and that is **proof the row applied in the live
deployment** — something no import and no static read can show. A route registered in the code but
answering `404` on the host is reported as `404:N`, which means the code is fine and the row is not
mounted. Note the difference from the handler calls of `verify`: `--live` drives the *running
deployment*, while the handler calls run inside the throwaway probe process, which is why those are on
by default.

Honest scope: the recording context is a **stub**, so what it shows is what the plugin's code registers,
not a simulation of the deployment — a service the plugin needs but the deployment lacks cannot be seen
from here, because the plugin just gets a stub. The stub is deliberately forgiving (an unknown service
method is callable and returns another stub, so `ctx.get("credentials").resolve(...)` does not die on the
probe's own gaps) but it is still a stub, and that shapes what a handler failure may be called — see
below. The empty config passed as the second argument models "an entry with an empty config block" (a
plugin that branches on `config === undefined` would otherwise look dead). `apply()` with a stub can
never prove boot-time success; `--live` and DSH's own log can. Packages with no server entry at all (a
client-only `dsh.client` bundle) are not executed — they are reported as `client`, and their code runs
in the browser.

**2. The `surface` column of `status` (menu item 1).**

| Value | Meaning |
|---|---|
| `live:N` | the running DSH serves N of the registered routes — it applied |
| `routes:N` | N surfaces `apply()` registered (proven by code, not by the host) |
| `hooks:N` | no route, but `apply()` reached for services, hooks or events |
| `client` | its behaviour is in the browser bundle; the server half is empty or absent |
| `declarative` | the entry imports but exports no `apply()` — nothing to run |
| `no-op` | `apply()` ran and registered nothing observable |
| `apply!` | `apply()` threw against the recording context |
| `404:N` | registered in the code, but the running DSH answers `404` |
| `shadowed` | the host component its client half draws into is disabled — see below |
| `shadowed?` | a disabled row's *name* appears in the client half — a lead, not a verdict |
| `wire:404` | its runtime calls address an endpoint this core does not serve — see below |
| `handler!` | a route it registered fails on its first call — a `ReferenceError` in the closure, see below |
| `handler?` | a handler warned, logged or threw something a stub can also produce — a lead, not a verdict |

**3. Handler calls — the route that catches its own error.** `apply()` only *registers* a route handler; the closure
is not executed until a request arrives. After `apply()` the probe therefore calls every harvested handler
once — `webServer.register({kind, path, handler})` — with a synthetic `GET`: a `req` that can be read,
iterated and listened to, and a recording `res`. It captures what the call threw, everything it logged
(level and text) and the answer it produced. The console is wrapped only for the duration of the call, and
the calls are sequential, so each handler's output belongs unambiguously to it.

This is the only thing that sees a *partial* failure. A plugin can import cleanly, apply cleanly,
register a route and answer a live `GET` with `200`, while the handler behind it is broken:

```
=== Route handlers that fail on the first call ===
  apply() only registers the closure; the probe calls each route handler once with a GET and reads
  what it threw and logged. A handler that catches its own error and answers 200 looks healthy elsewhere.

  example-plugin
    route: GET /example/items
    the first call logged a failure: [example-plugin] items route: read failed: ReferenceError: SOME_CACHE is not defined
      answered 200 "[]"
```

The declaration had been deleted in a refactor; the handler still read it, caught the error, logged it
and answered `200 []`. The surface showed an empty list and nothing else — not the boot log, not the
import probe, not `--live`, not the plugin's own working surface.

**Grading: `handler!` is a verdict, everything else is a lead.** The recording context is a stub, and a
stub produces failures of its own — `resolved.value.trim is not a function` when a proxy is not a string,
`ctx.get is not a function` before the stub was made forgiving, `ENOENT` for a cache file a handler falls
back on by design. Calling those breakage would cry wolf on a working deployment. So the verdict is
reserved for evidence the stub **cannot** have produced: a `ReferenceError`, because an undeclared binding
is undeclared under every context. Everything else — a `TypeError` mid-chain, a warning, an error-level
log, a call still running when the 1.5 s window closed, an `ENOENT` — is printed under
`=== Handler leads (not a verdict) ===` with the exact line, and the reader decides. The stub is also
deliberately forgiving: an unknown service method is callable and returns another stub, results chain, and
the request is async-iterable — so the probe does not blame the plugin for its own gaps.

Two guards keep the calls safe, and both are reported, never silent:

* A route whose path names a mutation (`delete`, `save`, `reset`, `update`, …) is **recorded but not
  called** — the probe only ever sends a bare `GET`. Each skip appears under `=== Probe notes ===`.
* At most 12 handlers per plugin, 1.5 s each, so a hanging handler becomes a "did not finish" lead rather
  than a stuck probe.

Everything under `=== Probe notes ===` is context, never a verdict, and each note is printed with its kind
so the sentence does not have to be decoded:

* `skipped by design` — the probe did not call the route (a mutating path, or the handler budget was
  reached). Nothing is known about it either way: neither a pass nor a failure.
* `stub may be the cause` — a `ctx.effect`/`ctx.inject` registration callback threw while the probe ran
  it, or a service lookup entered a branch the real host may skip. The message carries the explicit
  reminder that the recording stub is a plausible cause. The class of message the stub can produce is
  broad and includes Node's own argument validation tripped by a proxy — `The "path" argument must be of
  type string. Received function undefined` is a stub reaching `path.isAbsolute`, not a plugin bug.
* `probe error` — the probe could not run one registered handler. It says nothing about the plugin.

The stub is forgiving about **names** as well as values: `ctx.get(name)` answers with a stub for any name,
so a plugin that guards a block with such a lookup runs that block even where nothing provides the
service. The lookups are recorded (`lookups` in `--json`), and when one of them explains a callback
failure the report says so — naming the service, and stating that no probed plugin provides it and the
installed core never spells it either, so a real host resolves it to `undefined` and never enters that
branch. That is the shape of a failure that exists only inside the probe.

`verify --no-handlers` skips the calls entirely. That is a strictly weaker answer, so it is **not
cached**: a cache must never lose evidence a later reader would trust. `--json` carries the calls as
`handlers` (`path`, `error`, `logs`, `status`, `body`, `timedOut`, `ms`), the named lookups as `lookups`
and the notes as `notes` — the raw sentences, with `note_family()`/`note_line()` rendering them for a
reader.

Honest scope: this is not a real request. The handler sees `GET`, no body, no authentication and stub
services, so "the first call did not throw" is not proof that the handler works — only "it threw a
`ReferenceError`" is proof that it does not.

**4. Shadowed surfaces — when the UI host row is switched off.** The tool reads the **effective loader tree**:
every bundle's patch layer in bundle order, then the profile's own `cordis.patch.yml`. It knows which rows
exist, which are disabled, and *who disabled them*. Then it cross-references that with what each plugin's
client half actually touches. A plugin is reported only when three things hold together: its client half
really builds on a UI surface, the disabled row's id or module name appears in its code or comments
(*not* inside a user-facing string), and it is not itself the plugin that disabled that row.

The two ways that can be satisfied are **not equally strong**, and the report says which one it used:

* **`shadowed` — a verdict.** `package.json`'s `dsh.client.inject` names a module whose loader row is off.
  No heuristic is involved: the client half cannot be composed, and the plugin cannot work.
* **`shadowed?` — a lead.** The row's id or module name only *occurs in the text*, in code or a comment.
  A client half whose header says "the row ⋮ menu is rendered by the upstream `ui-workspace` component"
  matches this exactly as well as a real dependency does, so the line is printed with the finding and the
  reader checks it.

A lead is dropped from the column entirely once another enabled row in the *same layer that disabled the
row* reproduces every DOM name the client half selects on. That is the ordinary shape of a replaced UI
package: the replacement disables `ui-workspace` and mounts its own session list, whose rows carry the
same `role="treeitem"` and `*sessionRow` class the consumer looks for. The module is gone; the contract
is not. The finding is still recorded and printed — under a dim "not breakage" line, so the evidence
stays visible — but it is not an alarm.

```text
=== Shadowed surfaces ===
  example-plugin — the UI it augments is not mounted
    row: ui-workspace (@deepseek-ai/dsh-client-ui-workspace)
    disabled by: example-replacement
    evidence: package.json: dsh.client.inject names @deepseek-ai/dsh-client-ui-workspace

  what to do: either you do not need the plugin, or the component it augmented has to be re-enabled
  the whole tree, with the rows above marked: /usr/bin/python3 .../dsh_upgrade.py status --loader

  references explained by a replacement (not breakage):
    example-plugin names ui-workspace, and example-replacement re-mounts the same DOM contract
```

A lead that no replacement accounts for gets its own section, because it is worth a look and is not proof:

```text
=== Shadow leads (not a verdict) ===
  a switched-off row's name appears in the client half's text — a comment naming the component it augments reads the same way as a
  real dependency. Check the line before acting on it; the tree below shows what is actually mounted.

  example-plugin — the component it names may still be mounted
    row: ui-workspace (@deepseek-ai/dsh-client-ui-workspace) is off
    evidence: client.js:5: // The row ⋮ menu is rendered by the upstream ui-workspace component
    no enabled row in that layer reproduces the DOM names this client half selects on
```

Rows disabled with a `!!js` expression are reported as **conditional**, never as disabled — the loader
decides those, and this tool has no loader.

**Reading the tree (and why the hint is a full path).** The line at the end is the **exact command** that
reproduces the section: the interpreter that ran the tool, the absolute path of `dsh_upgrade.py`, and
`--profile` whenever the report was not made for the default profile. A bare `dsh_upgrade.py status
--loader` is a command that only works from the tool's directory, with a profile the reader has to guess —
which is why the report prints the full command instead. `--loader` is accepted by `status` **and** `verify`, so the
same flag works wherever the section appears, and when a plugin is shadowed the tree marks the rows that
matter:

```text
row           module                            state     disabled by            needed by
------------  --------------------------------  --------  ---------------------  -----------------
ui-workspace  @deepseek-ai/dsh-client-ui-works  disabled  example-replacement    example-plugin
chat          @deepseek-ai/dsh-client-ui-chat   on        @deepseek-ai/dsh-web   —
```

(A cell that does not fit wraps onto a continuation line — the table never truncates; the two module
names above are shortened here only to fit this page.)

The `needed by` column exists only when there is a shadowed plugin, and names the plugin that draws into
the switched-off row — the row *is* the answer to "what do I have to re-enable", and a bare list of rows
is not. A reference an enabled replacement already explains is deliberately left out of it: that plugin
draws into a mounted component, under a different name, and marking the old row would re-create the false
alarm `shadowed?` exists to avoid. `--json --loader` carries the same tree as data
(`loader.rows[].state` / `.neededBy`), so the flag is not silently dropped when the report is
machine-readable. In the menu it is one keypress: after a report that found a shadowed plugin, item 1 and
item 14 ask whether to print the tree and print it there, so the reader does not have to leave the menu,
find the script and remember the profile.

The authoritative answer to "did the row apply" is the running deployment's own plugin inventory — the
GUI's **Settings → Plugins**, served by `@deepseek-ai/dsh-host-plugin-inventory` as `pluginInventory.list`.
It needs the web token, so the tool uses the offline equivalent above plus the `--live` route probe.

**5. The wire contract — calls the core no longer serves.** The runtime probe imports
code; this check reads the *calls*. The installed core generates a TYPERT face per package that declares
every wire endpoint it serves as `namespace: '<ns>', method: '<method>'`, so the served set is read from
the core's own bytes — no host has to be running, and the read-only commands stay read-only. Each
installed plugin's client half and host entry are then scanned for literal `/api/...` paths in a file that
carries the Connection `client-request` envelope, and every path is classified against that set:

| Verdict | Meaning |
|---|---|
| `ok` | the path is an endpoint of the installed core (and the envelope's `method` agrees with it) |
| `dead` | it is not — with the successor named when only the separator changed |
| `mismatch` | the path is an endpoint, but the envelope sends a different `method`; the host rejects those |

```
=== Wire calls this core does not serve ===
  read from the plugin's own files against the core's declared endpoints (its TYPERT faces): the call answers 404 at run time

  example-plugin
    dead: /api/items.list  (method: "items.list")
      at client.js:72
      the legacy separator: this core serves "items/list" (the gateway claims <namespace>/<method>)
```

That is the failure shape this check exists for. The shared `/api` channel is claimed by the Typert
gateway, whose endpoints are `<namespace>/<method>`; a legacy `items.list` matches no interceptor, so the
POST answered `404`, the fetch threw, and the feature was never rendered — while the plugin imported
cleanly, applied cleanly, and served its own `/api-ext/items.delete` route. Nothing but the browser
console showed it.

Honest scope: only **literal** paths are read, so a URL assembled from variables is not followed and
nothing is reported for it; only files carrying the `client-request` envelope are treated as RPC callers,
so a bare `/api/...` path elsewhere is left to the route probe (those are ordinary webServer routes, and
`/api-ext/...` is the plugin's own extension surface by construction); and the endpoint set comes from
packages that publish a `typert` export. The check is static *because* it has to be: an unauthenticated
request to `/api` answers `401` before it routes, so from outside, a served path and an unserved one look
exactly alike.

`inspect` runs the same check **before** the artifact is installed, because that is the moment it is
cheapest to act on: a build can be clean, declare the right versions and pass every other check while
calling an endpoint the core no longer serves. The artifact is reduced to the same `name -> text` mapping
a directory and a tarball share, so `inspect ~/build/plugin.tgz` and `inspect ~/src/plugin` behave
identically, and a `.ts` source that has not been built yet is scanned just as well. A broken call makes
`inspect` exit `2` (the same code as an incompatible declaration) and appears as `wire` in `--json`.

One limit has no workaround and is reported rather than guessed at: when `--core` names a version other
than the installed one, the wire check is **not performed** — the endpoint set is read from the core's
generated faces under `node_modules`, which exist only for an installed core; a source checkout under
`--checkouts` publishes none. The report says so instead of inventing a verdict.

**6. The `loads` column.**

| Value | Meaning |
|---|---|
| `yes` | the server entry imported under the installed core |
| `no` | it does not import — the details are printed under the table |
| `client` | no server entry at all: a client-only bundle, covered by the static scans |
| `—` | not installed in the profile |
| `?` | not verified yet — run `verify` |

The plain `status` command is read-only and never executes plugin code: it shows what a previous
`verify` left in the cache. The menu's item 1 passes `--verify` and probes when the cache is missing
or stale. The cache is keyed by the probe schema plus the installed core version plus every plugin
version and manifest timestamp, so a tool upgrade and an upgrade of the core each refresh it exactly
once, and it is instant afterwards. The effective loader tree and the shadowed-surface section are read
from the profile's patch layers on every run — no probe, no cache, always current.

## Plugins built by yourself (present neither in npm nor on GitHub)

The tool checks such plugins in full — both when they are already installed and when they are detached:

| What | How it works |
|---|---|
| Manifest | read from the tarball (`file:…tgz`) or the directory (`link:…`), not from the registry |
| Declarations (check 1) | `peerDependencies`/`engines.dsh` from that manifest |
| Core delta (checks 2–6) | `require`/`import` scans run over the artifact's code; there is no need to unpack the tarball |
| Snapshot and incompatible list | store `manifest` and `localPath`, so `recheck`/`attach` work without the registry |
| Installation | installs exactly the recorded specifier (`file:…tgz` / `link:…`), the version is not substituted |
| Version update (`--update`) | impossible by definition: it has to be updated in its own repository and the artifact rebuilt |

What the tool does in non-standard cases:

* **Artifact not found** (the tarball was deleted/moved) — the plugin is marked explicitly
  (`local source not found: <path>`) and is not installed by `attach`: "there is nothing to install".
  For this, the record carries the `installable: false` marker.
* **Name taken in npm** — if you publish a different artifact under the same name, this is shown
  as a note under `--verbose` ("npm has X — that is a DIFFERENT artifact"), but it does not affect
  the verdict or the installation, and the local artifact is never replaced by the npm one.
* **Repository directory** — scanned without `node_modules`, `.git`, `tests/`, `examples/`;
  therefore a `link:` to a working repository does not produce false positives on test fixtures.

A practical order for your own builds: rebuild the tarball for the new core → put it at the same
path (or fix the specifier) → `check --core <version>` → `recheck --install --yes`.

### Checking an artifact BEFORE installing it

`check` only sees what is already in the profile. `inspect` takes the artifact directly, so a
build can be judged — and rejected — before it ever reaches `node_modules`:

```bash
python3 dsh_upgrade.py inspect ~/build/my-plugin-0.1.5-rc.2.tgz    # a built tarball
python3 dsh_upgrade.py inspect ~/src/my-plugin                     # a repository directory
python3 dsh_upgrade.py inspect file:~/build/my-plugin.tgz --since 0.1.1-rc.2   # specifier form
```

* **The profile is neither read nor written**; nothing is installed. The artifact is the only
  input, which is what makes it safe to point at a build you have not decided about yet.
* In the menu (item 13) the path is typed with completion: **Tab** lists and completes files and
  directories, spaces are escaped for you, and `~` is expanded. Quoting or escaping by hand works
  just as well.
* **The target is the INSTALLED core** by default — the question here is "will this run on the
  harness I have", not "what is the newest release". `--core V` asks about another version.
* **`--since OLD`** sets the baseline of the removed-package scan. Against the installed core that
  comparison is self-referential (`X - X = ∅`) and proves nothing; naming the release your plugin
  was written for turns it into a real check.
* Exit codes match `check`: `0` — nothing proven incompatible, `2` — incompatible, `1` — the path
  or the manifest could not be read. With `--json` stdout is a SINGLE JSON document (the analysis
  progress goes to stderr), so it can be piped straight into a script.
* Because the artifact has not run on any core, the **empirical** promotion never applies here: a
  strict declaration stays "unconfirmed" until the plugin is actually installed and working.

## Safety

* **Plugin data is not deleted.** Detaching is a `pnpm remove` in the profile directory (exactly
  what `dsh plugin --profile web remove` does); it cleans only `node_modules`. The data lives in
  `~/.dsh` (the shared `sessions`, `storages`, `skills`, `.agent-presets`, `settings.yaml`,
  `.credentials.yaml`, plus any directories plugins create under it) and stays in place —
  the `detach` command shows a map of it before the operation.
* **The snapshot is always written before any changes** and contains, for every plugin, the name,
  the specifier, the source, the version, whether it is present in `dsh.profile.bundles`, and the
  full `package.json`. The snapshot can be used even after the packages have been detached: the
  manifest is inside.
* **Exact versions only.** On reinstallation an npm package is installed as `name@version`, not by
  the recorded range: a range like `^0.3.16` would pull the newest version, which may require a
  different core (a real case: the newest release of a plugin may raise its own minimum core).
* **Upgrades only.** `--update` raises the version only if the new one passes the check; there are
  no downgrades.
* **Post-check.** After installation the code checks are run again — now against the files that
  are actually installed; the ones that fail land in the list (and are detached with `--prune-failed`).

## State files

```
state/
├── snapshots/<date>-<profile>.json|.md   # snapshots of "what was installed"
├── incompatible-<core>.json|.md          # the incompatible list (read by recheck)
├── check-<core>.json                     # detailed report of the last check
├── verified-<profile>.json               # runtime verification verdicts (read by status)
│                                         # keyed by probe schema + core + every plugin copy
└── cache/                                # cache of registry and marketplace index responses
```

The **settings file** (the options remembered between runs) is separate and lives in the user
configuration directory, not here — see "Settings are remembered" above.

Example of a list: `incompatible-0.1.5-rc.2.md` — a table with the status (`NO` — proven
incompatibility, `??` — unconfirmed), the reason, the requirement and the commands for what to do next.

## Environment variables

| Variable | Meaning |
|---|---|
| `DSH_HOME` | Root of the DSH data (default `~/.dsh`). |
| `DSH_INSTALL_DIR` | Directory of the installed core (otherwise it is looked up via `dsh` from PATH). |
| `DSH_CHECKOUTS_ROOT` | Where version checkouts live: `temp` (the default) keeps them under the system temp directory, reused between runs and cleared on reboot; `keep` is `<DSH_HOME>/checkouts`; anything else is a directory to keep them in. It **beats the saved setting** and loses only to `--checkouts` given on that run. |
| `DSH_UPGRADE_STATE` | State directory (or the `--state-dir` flag). |
| `DSH_UPGRADE_CONFIG` | Settings file (default `~/.config/dsh-upgrade/config.json`). |
| `DSH_UPGRADE_COLOR` | Color mode: `auto` (default), `always`, `never`. The `--color` flag wins over it. |
| `NO_COLOR` | When set (to any non-empty value), colors are disabled in `auto` mode. |

## Checks and tests

```bash
python3 -m unittest discover -s tests -v          # 381 tests: semver, declarations, scans, registrations, local builds, core layouts, menu, settings, checkouts, verification, route-handler calls, the effective loader tree, shadowed surfaces, wire contracts, pasteable hints, completion, tables, target
python3 dsh_upgrade.py check --core 0.1.5-rc.2   # exit code 2 if there are incompatible plugins
```

`check` returns `0` when everything is compatible, and `2` when there are incompatible plugins —
handy for scripts.

The suite is self-contained: tests that need a real core checkout, or a pair of real plugin
artifacts, skip when those are not present. To run the artifact regression pair, point
`DSH_UPGRADE_TEST_BROKEN_TGZ` and `DSH_UPGRADE_TEST_FIXED_TGZ` at two builds of one client plugin
— the broken one inlining the self-registering row `@deepseek-ai/dsh-api-session-controller` —
and `DSH_CHECKOUTS_ROOT` at a directory holding the core checkouts.

## Limitations

* The core upgrades itself only with the `pipeline --run-core-upgrade` flag (`npm i -g …`); by
  default the command is printed, because installing the core writes outside the workspace and
  replaces the running runtime.
* Inline purity (check 6) reads what a bundle inlined from its `//#region node_modules/…` markers —
  the record tsdown/rolldown emit. A bundle built by another tool, or minified enough to drop the
  markers, has nothing to read: the check then stays silent rather than guessing (a false "NO" would
  be worse — it would reject a working artifact). Its unambiguous half is untouched by this: a
  bundle that inlines a self-registering client row is still caught by check 4, which reads the
  facade call itself.
* For npm plugins the code checks are visible only when the plugin is installed (or after
  installation). Once it has been detached, only declarations work for it — which is exactly why
  the manifest is stored in the snapshot. For local builds (`file:`/`link:`) the scans run over the
  artifact itself and work at all times, as long as the file is in place.
* For plugins with a local source (`file:`/`link:`) a version update is impossible: they have to be
  rebuilt in their own repository (see the section "Plugins built by yourself").
* **`verify` executing `apply()` cannot prove a plugin works.** The context it builds is a stub: it
  records what the plugin registers, but a service the plugin needs and the deployment lacks simply
  arrives as a stub, so a row the loader would leave `pending` can still look busy. What the probe
  *can* prove is the reverse and the concrete: that `apply()` throws, that it registers nothing, that a
  route handler throws a `ReferenceError` on its first call, and — with `--live` — that the surfaces it
  claims are really served by the running host. The final word on "did the row apply" is DSH's own boot
  log, the GUI's Settings → Plugins, and `--live`.
* **Calling a route handler is not a real request, and only a `ReferenceError` is a verdict.** The
  handler sees `GET`, an empty body, no authentication, stub services and a synthetic response, so "the
  first call did not throw" is not proof that it works. The recorded call is graded accordingly: a
  `ReferenceError` cannot be the stub's doing (an undeclared binding is undeclared under any context) and
  is a `handler!` verdict; a `TypeError` mid-chain, an `ENOENT` for a file the handler falls back on by
  design, a warning, an error-level log or a call still running when the 1.5 s window closes is printed
  as a `handler?` lead with the exact line, because the stub can produce those too. Routes whose path
  names a mutation are recorded but never called — the probe only sends a bare `GET` — and every skip is
  reported under `=== Probe notes ===`. `--no-handlers` skips the calls altogether; that verdict is
  deliberately not cached.
* **The patch reader is not a YAML parser.** It reads the restricted structure DSH actually uses — a
  top-level list of entries, each either `insert: [...]` or an id-targeted override — and only the keys
  that decide enablement. Anchors, flow collections and multiline scalars are not implemented;
  `config:` subtrees are deliberately ignored, so a key named `name:` inside one is not mistaken for a
  row. A row disabled with a `!!js` expression is reported as *conditional*, never as disabled: the
  loader decides those, and this tool has no loader.
* **A shadowed surface is a report, not a verdict.** The detector needs three things to hold together
  (a client half that builds on a UI surface, the disabled row named in its code or comments outside a
  user-facing string, and the plugin not being the one that disabled that row) and prints the evidence
  line it matched. Read the line: it is proof of a textual reference, not an execution trace. That is why
  a code match prints as `shadowed?` and only a `dsh.client.inject` naming a disabled module prints as
  `shadowed`. The automatic downgrade — an enabled row in the same layer reproducing the consumer's DOM
  names — is a heuristic too, and it fails in the safe direction: an unrecognised contract stays a lead.
* **The wire check reads literals, and only the core's own faces.** A path built from variables inside
  the plugin is not followed, a call made outside a file carrying the `client-request` envelope is not
  treated as RPC, and an endpoint served by something other than a published `typert` face is not in the
  compared set. It answers "is this path one the installed core declares", which is the question the
  404 raises, not "will this call succeed".
* **`--live` reaches real handlers.** The probe is a plain `GET` with no body, but it does run the
  plugin's own route handler when one is registered. That is why it is opt-in, and why it is a separate
  flag rather than part of the default `verify` — it drives the *running deployment*. The handler calls
  inside `verify` are different: they run in the throwaway probe process, so they are on by default
  (`--no-handlers` turns them off). The wire check does not touch the host at all: an unauthenticated
  `/api` request answers `401` before it routes.

## Structure

```
dsh_upgrade.py             # CLI with all subcommands + opening the menu without arguments
scripts/                   # wrappers for each subcommand
dshupgrade/
├── paths.py               # DSH home, profile, core directory (nested or hoisted), checkout locations, state
├── config.py              # the settings file: options remembered between runs
├── invocation.py          # the exact command that reproduces a report (pasteable hints)
├── completion.py          # terminal-grade path input (Tab completion, unescaping)
├── semver.py              # prerelease-aware version arithmetic
├── registry.py            # npm registry and marketplace index (urllib + cache)
├── locals.py              # local builds: file:/link:/tgz, manifest and code from the artifact
├── menu.py                # interactive menu (the same one that opens without arguments)
├── host.py                # inventory of host packages
├── compat.py              # the six compatibility checks
├── checkout.py            # checkout of a core version and facts from its tree
├── profile.py             # reading the profile, detaching and installing plugins
├── effects.py             # effective loader tree: which rows exist, who disabled them,
│                          # and which plugins draw into a row that is switched off
├── wire.py                # the calls a plugin makes vs the endpoints the installed
│                          # core declares (its generated TYPERT faces)
├── verify.py              # runtime probe: imports each plugin and calls its apply()
├── snapshot.py            # snapshots and incompatible lists
├── analysis.py            # assembling verdicts
├── style.py               # colors, terminal width, text wrapping
└── report.py              # width-aware tables and grouped verdicts
tests/                     # unittest (semver, locals, menu, settings, checkouts,
                           #   completion, report tables, target, registrations,
                           #   declaration integrity, inline purity, core layouts,
                           #   verification, wire contracts, hints and the loader tree)
state/                     # snapshots, lists, verification verdicts, cache
```

## Attribution

The compatibility counting in check 1 is a deliberate **re-implementation of the marketplace
plugin's engine**, not an import of it. That plugin —
[`dshmarket`](https://github.com/dsh-market/dsh-market) (npm `dshmarket`, MIT, site
[dshmarket.com](https://dshmarket.com)) — is what the harness itself uses to judge plugin
compatibility. It cannot be reused here: it is Node code, and during an upgrade it is detached
together with every other plugin, so the tool that decides what to reinstall would depend on
something it has just removed.

The rules are therefore ported one-to-one into stdlib Python, so that this tool's verdict and
the marketplace verdict agree:

| Here | Upstream (`dshmarket`) |
| --- | --- |
| `compat.declarations_for` | `manifestFacts` plus the host-package filter of `deriveHostCompatibility` (`lib/discovery-compatibility.js`) |
| `compat.evaluate` | `deriveHostCompatibility` |
| `compat.classify_failure` — `below-min`, `exact-pin`, `above-explicit-max`, `above-implicit-ceiling` | `classifyPeer` (`lib/compatibility.js`): `belowMin` / `aboveMax` count as a failure only with an explicit upper bound or an exact pin; a newer host above an implicit caret/tilde ceiling is a warning, not a failure |
| `semver.satisfies` | `satisfiesRange(version, range, { includePrerelease: true })` (`lib/check.js`) |
| `host.host_inventory` | the marketplace host-package policy behind `dshHostInfo()` (`lib/routes.js`) |
| `registry.MARKET_INDEX` | the same public catalog, `awesome-dsh-plugin.com/plugins.json` (`lib/catalog-npm.js`) |

Nothing is vendored from the plugin — this is an independent Python implementation — but the
rule set is theirs, and check 1 is meant to reproduce their verdict exactly. Thanks to the
`dshmarket` authors.

## Related and prior art

Other projects in the same problem space — independent of this tool, listed without comparison or
endorsement. They differ in language, scope and the point at which they act (inside the running
harness, or outside it as this tool does):

* [Shizuku-keop/dsh-compat-guard](https://github.com/Shizuku-keop/dsh-compat-guard)
  (`dsh-compat-guard`) — Node CLI: an upgrade pre-flight gate, storage-format fingerprinting,
  `$DSH_HOME` backup, session migration, a per-profile lockfile and a plugin × DSH compatibility
  matrix.
* [whyihaveyou/dsh-suite](https://github.com/whyihaveyou/dsh-suite) — plugin discovery,
  compatibility matrices, SQLite snapshots and upgrade diffs (`compatibility-radar`).
* [zzy6-a/dsh-upgrade-guard](https://github.com/zzy6-a/dsh-upgrade-guard) — post-upgrade plugin
  compatibility patrol with repair/disable and an out-of-host supervisor rescue.
* [oh-my-dsh/dsh-plugin-upgrade-skill](https://github.com/oh-my-dsh/dsh-plugin-upgrade-skill) — a
  skill that helps plugins keep up with dsh version upgrades.
* [ybl2020/dsh-upgrade](https://github.com/ybl2020/dsh-upgrade) — an upgrade-process skill
  (assessment → approval → upgrade → verification) with backup and rollback notes.
* [william-jin-cmu/dsh-plugin-upgrade](https://github.com/william-jin-cmu/dsh-plugin-upgrade) —
  skill + scripts for moving plugins across framework releases.
* [`@linxin666/dsh-doctor`](https://github.com/zhu1090093659/dsh-web) — transactional rescue mode,
  an isolated recovery capsule and rollback for DSH profiles.
* [`@xiaoyuyu6420/dsh-backup`](https://github.com/xiaoyuyu6420/dsh-backup) — backup/restore and
  GitHub sync of `~/.dsh`, including upgrade snapshots.

Thanks to the DeepSeek Harness plugin community; the projects above are the work of their own
authors.

## License

MIT. See [LICENSE](LICENSE).
