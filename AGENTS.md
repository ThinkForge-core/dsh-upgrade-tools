# dsh-upgrade-tools: guide for agents

A short map for an automated reader that drives `dsh_upgrade.py`. `README.md` is the full manual
written for a person; this file covers only what an agent needs before touching anything: which
commands are safe, what the exit codes mean, how to read a verdict, and where the trust ends.

Run everything from a plain Python 3 interpreter — standard library only, no Node and no npm:

```sh
python3 dsh_upgrade.py <command> [flags]
```

A flag is accepted both before and after the subcommand (`--profile web status` is the same as
`status --profile web`). The subcommand itself is optional: with no arguments, or with `menu`, the
tool opens an interactive menu. A non-interactive caller (no TTY) is not prompted — it gets the
action map printed instead, destructive actions marked `!`, and exit `0`, so `menu` is a cheap way
to see what exists.

## Flags

`--json` is accepted by every command; `--profile`, `--core`, `--verbose`, `--offline`, `--no-clone`,
`--state-dir`, `--checkouts` and `--prune-checkouts` too. The rest are per command:

| flag | command | effect |
|---|---|---|
| `--summary` | `status`, `check`, `verify` | one line of counts instead of the full report |
| `--verify` | `status` | fill in the `loads` column by importing every installed plugin; without it the column shows the cached verdicts and nothing is executed |
| `--loader` | `status`, `verify` | print the effective loader tree: every row, what mounts it, who disabled it |
| `--cached` / `--live` | `verify` | reuse cached verdicts / also `GET` every probe route on the running DSH |
| `--no-handlers` | `verify` | do not call the route handlers: a weaker probe, and the result is not cached |
| `--web-url URL` | `verify` | where `dsh web` serves, for `--live` |
| `--diff PATH` | `verify` | compare with a saved `state/verified-<profile>.json`; excludes `--summary` |
| `--limit N` / `--all` | `plan` | how many recent new core versions to walk |
| `--update` | `check` and the destructive commands | see below — the meaning depends on the command |
| `--since V` | `inspect` | older core that shipped the packages the artifact may still reference |
| `--only NAME…` | `attach`, `detach`, `plugins` | act on only these plugins |
| `--install-unknown` | `attach`, `recheck`, `pipeline`, `plugins` | also install/reinstall the unconfirmed ones, with a post-install code check |
| `--detach-first` | `plugins` | remove each plugin before installing its new version; by default the new version is installed over the current copy |
| `--dry-run` / `--yes` | destructive | print the plan and change nothing / confirm the operation |
| `--termux` / `--no-termux` | all | force the Termux/Android correction on or off; the default detects Termux |
| `--termux-dir DIR` | all | where the Termux patch layer lives; the default discovers it, then clones the fork |

## The Termux correction

On Termux the core upgrade is not complete when `npm i -g` returns: that command
restores the **pristine upstream tree**, which does not run on Android (sepolicy
denies `link(2)`, Bionic has no `flock(2)`, `sharp` has no android-arm64 build).
The Android corrections live in a separate layer — the
`deepseek-harness-termux` fork — whose anchor-based patcher is re-applied after
every core install.

`dsh-upgrade-tools` handles this itself, so nothing here is a step an agent has
to remember:

* the layer is discovered (or the fork is cloned to `$DSH_HOME/termux-layer`) and
  reported in `status` (`core.termux` in `--json`) and in the menu settings;
* the automatic target is capped to the version the layer validates
  (`VALIDATED_DSH_VERSION` in its `install.sh`), because the patcher matches
  upstream by exact anchors and a core past that version cannot be patched. The
  cap never moves backwards;
* the pipeline's core-upgrade seam prints — and with `--run-core-upgrade` runs —
  `fix-dsh-runtime.sh` **between** `npm i -g` and `attach`, which is the correct
  order: a plugin judged against an unpatched core is judged against a core that
  cannot write a file on that platform. If the native addons no longer load, the
  layer's installer rebuilds them.

Consequences for an agent:

* `--run-core-upgrade` on Termux now also patches the core and may take several
  minutes when the natives have to be rebuilt. That is expected, not a hang.
* Without a layer the step degrades to a printed instruction plus a warning —
  never to silence.
* `--no-termux` is for a deliberate pristine core; do not pass it on Android.

Two flags and one command are easy to misread, and all three decide what you can actually do:

* **`--update` means "consider newer versions", and what it does depends on the command.** On the
  read-only `check` it only *surfaces* candidate versions — the `latest` column of the table is
  filled in, nothing installed. On `attach`, `recheck` and `pipeline` it installs, for each npm
  plugin, the newest version that evaluates `compatible`, strictly upward (never a rollback) and
  never one proven `incompatible`. `plugins` always considers newer versions, so the flag is a no-op
  there — and with `plugins --install-unknown` the opt-in is what lets it install a newer version
  that is merely `??` (never a proven `incompatible` one). So a newer version visible under
  `check --update` is not automatically a version the tool will install for you.
* **`--only` names plugins, exactly.** `detach --only X` detaches X alone (the snapshot still records
  the whole profile), `plugins --only X` updates X alone and leaves every other plugin untouched, and
  `attach --only X` restores X from the snapshot. A name the profile does not have is an error listing
  the installed ones, never a silent no-op. What `--only` cannot do is invent a plugin: to add one
  that is not in the profile nor in a snapshot, use the core CLI
  (`dsh plugin --profile web add name@version`).
* **`plugins` updates, and touches nothing else.** It acts only on the plugins that have a newer
  version, so a plugin with nothing newer is not detached, not reinstalled and not dropped — a run
  can never leave one out of the profile. Reinstalling the whole profile is what `detach` followed by
  `attach` is for. Every name lands in exactly one of three groups, and the report names each one with
  its reason: *to update* (the version that will be installed), *held back* (`1.8.0` is published but
  not confirmed for this core; or proven `incompatible`, which no flag installs) and *left alone*
  ("no newer version is published", "a `link` source — no registry version to compare"). Selecting a
  version means comparing versions, so the registry is always consulted and only an npm source can
  have an update. The version chosen is pinned, so the plan the reader approved and the artifact that
  gets installed cannot disagree — including when the installed copy itself is `??`.

## Read-only commands

`status`, `check`, `plan`, `inspect`, `verify`, `core-versions` — none of them changes the profile,
the installed core or plugin data. They read the profile, the installed core, the npm registry and,
for the version checks, a checkout of the target core.

Read-only is a statement about the profile, not about the disk: they do write reports into the state
directory, and `check` *always* writes the incompatible list (even when nothing is incompatible) —
that file is the state `recheck` and the menu viewer consume. `state/incompatible-<core>.json|.md`,
`check --json` also `state/check-<core>.json`, `verify` `state/verified-<profile>.json`, the registry
cache `state/cache/`, snapshots `state/snapshots/`.

* `status` — current core, and per plugin what its installed copy does (`loads`, `surface`). It
  reports the verdicts a previous `verify` left in the cache and never executes plugin code unless
  `--verify` is passed, so it is cheap either way.
* `check --core V` — compatibility of every profile plugin with version `V`. Plugins that the cached
  probe has proven on the installed core are graded `verified` instead of `compatible`/`unknown`.
* `plan` — every new core version and what happens to plugins on each (declarations only).
* `inspect PATH` — compatibility of an artifact that is not installed (directory or `.tgz`).
* `verify` — imports the installed copies with `node`, calls `apply()`, then calls the route
  handlers it registered and reads what they threw and logged. This is what produces the verdicts
  `check` and `status` later read.
* `core-versions` — available core versions and tags.

For `status`, `check`, `verify` and `inspect`, `--json` puts exactly one JSON document on stdout and
sends the human report and progress lines to stderr. `status`, `check` and `verify` also accept
`--summary` (one line of counts; a JSON object with `--json`). `check`'s summary carries an extra
`verified` counter and says so in the line; `status` and `verify` report on the probe itself and omit
it. `verify --diff PATH` compares the current run with a result saved earlier and prints only the
differences.

## Destructive commands (need `--yes`)

`detach`, `attach`, `pipeline`, `plugins`, `recheck --install` change the profile: they
remove plugins, install plugins or rewrite `node_modules`. Preview with `--dry-run` first, then
repeat the same command with `--yes`. Run them only on an explicit human instruction, and note that
a core downgrade is not supported — session state is versioned monotonically.

## Exit codes

| code | meaning |
|---|---|
| `0` | nothing proven incompatible; nothing failed |
| `1` | an input could not be read (profile path, manifest, `--diff` baseline) or a `--core` names no version the tool can obtain |
| `2` | a plugin is proven incompatible (`check`, `inspect`) or a server entry does not load (`verify`) |

`check` returns `2` when any plugin is incompatible — the contract `recheck` and the pipeline rely
on. An unconfirmed (`??`) plugin does not by itself make the code `2`. A destructive command
previewed with `--dry-run` exits `0`, not `1`: nothing failed.

A `--core` that is not a published version, not the installed core and not a checkout on disk is
refused with `1` and a message naming the newest published version. This is deliberate: comparing
against a version that does not exist would report every plugin as `??` for lack of anything to
compare with, which reads like a result and is not one.

## Reason codes

`check --json` and `inspect --json` carry `reason_code` next to the human readable `reason` of every
plugin, so a finding can be classified without parsing prose. A plugin that fails several checks at
once carries the code of the most fundamental one; the detailed hits stay in the entry
(`removed_hits`, `client_hits`, `declaration_hits`, `inline_hits`, `registration_hits`).

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

`reason_code` is `null` when no check produced a finding with an identifier: a compatible plugin, or
a manifest that could not be read at all. The last two wire codes appear in `inspect` only; `check`
reports no wire finding of its own (it consults the wire scan only to withhold the `✓` grade from a
plugin whose call would go nowhere).

## Trust boundaries

* `handler!` — a verdict. The handler threw a `ReferenceError`, which the recording stub cannot have
  caused.
* `shadowed` — a verdict. The manifest itself names a module whose loader row is off.
* `wire:404` — a verdict. Read from the core's own generated endpoint declarations.
* `handler?` — a lead. The handler warned, logged a failure the stub can also produce, or was still
  running when the probe window closed.
* `shadowed?` — a lead. The disabled row's name merely occurs in the client half's text.
* `✓` (verified) — a verdict, and the strongest one, but a narrow one: a cached `verify` run proved
  this installed copy imports, applies and answers its handlers, and no other check objected. It is
  granted only when the target IS the installed core, because a runtime verdict is evidence about one
  core version; for any other `--core` the entry keeps its declaration status. Nothing is executed to
  grant it, so it is only as fresh as the last `verify` — a plugin or core change drops the cached
  verdict and the plugin falls back to `??`/`ok` until `verify` runs again. It never overrides a
  proven `NO`, and it is not a substitute for `--live`.
* `??` (unknown) — "nothing declared to compare with", not "compatible" and not "broken". A plugin
  the installed core has verified is reported as `✓`, not as `??`.
* `↑` in the `version` column — an available update, shown only when the registry was queried
  (`check --update`; `plugins` always queries it). Green/bold means the newest published version
  evaluates `compatible` and the tool would install it by itself; yellow means a newer version exists
  but was NOT confirmed, so `plugins` holds it back and installs it only under `--install-unknown`
  (never one proven `incompatible`). The marker never means "safe to install" on its own — read the
  colour. Without `--update` the column is empty for lack of data, not because every plugin is
  current, and the report says which of the two it is.
* `blocks_core_upgrade: false` — the pipeline upgrades the core and holds the plugin back; `true`
  means the target core itself is out of reach (an older version than the installed one).
* `verify` runs plugin code against a stub context. A failure the stub can produce on its own is
  reported as a lead, with that note; only stub-immune evidence is a verdict.
* `=== Probe notes ===` — never a verdict. Each note is printed with its kind: `skipped by design` (the
  probe deliberately did not call the route), `stub may be the cause` (a `ctx.effect`/`ctx.inject`
  registration callback threw, or a `ctx.get` lookup entered a branch a real host would skip) and
  `probe error` (the probe itself gave up). `note_family()` and `note_line()` classify and render a note;
  `notes` in `--json` stays the probe's raw sentence, and `lookups` names every service that was looked up.

The two families come from different commands, so do not look for one where the other lives: the
`surface` tokens and the probe notes are rendered by `status` (fed by the last `verify` run, or by
its own with `status --verify`) and by `verify`; `reason_code` is a `check`/`inspect` field.

In `verify --json` the `findings` list carries every finding with its `confidence`
(`"verdict"` or `"lead"`), so the two kinds do not have to be told apart by the surface text.

## What not to do

* Do not run a destructive command without an explicit instruction from the human.
* Do not read `??` as "compatible": it means there is nothing to compare.
* Do not read `✓` as "it will work on the target core": it is evidence about the installed core
  only, and only as fresh as the last `verify`.
* Do not read `blocks_core_upgrade: false` as a blocking condition — it is the opposite.
* Do not treat `handler?` or `shadowed?` as diagnoses; confirm the cause in the plugin's source.
* Do not decide the upgrade. The tool reports facts; the human chooses.
