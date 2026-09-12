# dsh-upgrade-tools: guide for agents

A short map for an automated reader that drives `dsh_upgrade.py`. `README.md` is the full manual
written for a person; this file covers only what an agent needs before touching anything: which
commands are safe, what the exit codes mean, how to read a verdict, and where the trust ends.

Run everything from a plain Python 3 interpreter — standard library only, no Node and no npm:

```sh
python3 dsh_upgrade.py <command> [flags]
```

## Read-only commands

`status`, `check`, `plan`, `inspect`, `verify`, `core-versions` — none of them changes the profile,
the installed core or plugin data. They read the profile, the installed core, the npm registry and,
for the version checks, a checkout of the target core.

* `status` — current core, and per plugin what its installed copy does (`loads`, `surface`).
* `check --core V` — compatibility of every profile plugin with version `V`.
* `plan` — every new core version and what happens to plugins on each (declarations only).
* `inspect PATH` — compatibility of an artifact that is not installed (directory or `.tgz`).
* `verify` — imports the installed copies with `node`, calls `apply()`, then calls the route
  handlers it registered and reads what they threw and logged.
* `core-versions` — available core versions and tags.

`--json` is accepted by all of them. For `status`, `check`, `verify` and `inspect`, stdout then
carries exactly one JSON document and the human report and progress lines go to stderr. `status`,
`check` and `verify` also accept `--summary` (one line of counts; a JSON object with `--json`).
`verify --diff PATH` compares the current run with a result saved earlier and prints only the
differences.

## Destructive commands (need `--yes`)

`detach`, `attach`, `pipeline`, `plugins --update`, `recheck --install` change the profile: they
remove plugins, install plugins or rewrite `node_modules`. Each one previews with `--dry-run`
first. Run them only on an explicit human instruction, and note that a core downgrade is not
supported — session state is versioned monotonically.

## Exit codes

| code | meaning |
|---|---|
| `0` | nothing proven incompatible; nothing failed |
| `1` | an input could not be read (profile path, manifest, `--diff` baseline) |
| `2` | a plugin is proven incompatible (`check`, `inspect`) or a server entry does not load (`verify`) |

`check` returns `2` when any plugin is incompatible — the contract `recheck` and the pipeline rely
on. An unconfirmed (`??`) plugin does not by itself make the code `2`.

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
runs no wire scan.

## Trust boundaries

* `handler!` — a verdict. The handler threw a `ReferenceError`, which the recording stub cannot have
  caused.
* `shadowed` — a verdict. The manifest itself names a module whose loader row is off.
* `wire:404` — a verdict. Read from the core's own generated endpoint declarations.
* `handler?` — a lead. The handler warned, logged a failure the stub can also produce, or was still
  running when the probe window closed.
* `shadowed?` — a lead. The disabled row's name merely occurs in the client half's text.
* `??` (unknown) — "nothing declared to compare with", not "compatible" and not "broken".
* `blocks_core_upgrade: false` — the pipeline upgrades the core and holds the plugin back; `true`
  means the target core itself is out of reach (an older version than the installed one).
* `verify` runs plugin code against a stub context. A failure the stub can produce on its own is
  reported as a lead, with that note; only stub-immune evidence is a verdict.

In `verify --json` the `findings` list carries every finding with its `confidence`
(`"verdict"` or `"lead"`), so the two kinds do not have to be told apart by the surface text.

## What not to do

* Do not run a destructive command without an explicit instruction from the human.
* Do not read `??` as "compatible": it means there is nothing to compare.
* Do not read `blocks_core_upgrade: false` as a blocking condition — it is the opposite.
* Do not treat `handler?` or `shadowed?` as diagnoses; confirm the cause in the plugin's source.
* Do not decide the upgrade. The tool reports facts; the human chooses.
