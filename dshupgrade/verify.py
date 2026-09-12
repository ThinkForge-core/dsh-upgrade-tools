"""Runtime verification: does the INSTALLED copy of a plugin actually work?

Declarations answer "may this plugin run on that core"; this module answers a
different question — "does its code import, apply, and survive its first request on
the core that is installed right now". That is the check that catches the failures
the declarations miss: a ``require``/``import`` of a ``@deepseek-ai/<pkg>`` the core
no longer ships, a peer dependency that cannot be resolved, an ``exports`` map that
stopped resolving, a syntax error in the entry file — and, one level deeper, a
registered route handler whose closure is broken.

How it works: the plugin's entry is imported by a **separate Node process** whose
working directory is the profile directory, so the module graph is resolved exactly
the way DSH resolves it at boot (``node_modules`` of the profile, pnpm links and
all). One process per plugin, run in parallel, with a timeout. In that process the
probe calls ``apply()`` against a recording context, and then calls every route
handler ``apply()`` registered once, with a synthetic GET, reading what it threw and
logged.

Scope, stated honestly:

* The probe **imports** the entry point, calls ``apply()`` against a stub context,
  and calls registered route handlers with a synthetic request. It does not boot
  the plugin the way a real deployment does: a service the deployment lacks arrives
  as a stub, and an event handler (``ctx.on``) is never fired.
* Packages that are **client-only** (no server entry at all — just a ``dsh.client``
  bundle) are not executed here: their code runs in the browser. For them only the
  static scans of :mod:`dshupgrade.compat` apply, and the probe says so.
* A clean run is evidence, not a guarantee. The final word is still DSH's own boot
  log and the GUI.

The results are cached in the state directory, keyed by the installed core version
plus every plugin version and manifest timestamp, so a status command after an
upgrade re-probes automatically and a status command right after that is instant.
A run that skipped the handler calls is deliberately not cached.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import effects as effects_mod
from .paths import core_install_dir, host_modules_dir, read_json, state_dir

#: The whole result is one machine-readable line on stdout, prefixed with this.
PROBE_MARKER = "__DSH_PROBE__"

#: One plugin per process; these run in parallel.
DEFAULT_JOBS = 8
PROBE_TIMEOUT = 90

#: Bumped whenever the probe starts measuring something new. It is part of the
#: cache fingerprint, so a schema bump re-probes instead of trusting verdicts that
#: answer a weaker question. The route-handler calls are part of the current
#: schema: a verdict without them says "the handler was registered", never "the
#: handler runs".
PROBE_SCHEMA = 4

#: Statuses.
LOADS = "loads"          # the server entry imported
FAILED = "failed"        # it threw
CLIENT_ONLY = "client"   # no server entry: the code runs in the browser
MISSING = "missing"      # not installed in the profile
UNAVAILABLE = "unavailable"  # no node, or the probe could not run

#: What the ``loads`` column shows.
LABELS = {
    LOADS: "yes",
    FAILED: "no",
    CLIENT_ONLY: "client",
    MISSING: "—",
    UNAVAILABLE: "?",
}

#: One line of explanation per status (for the legend under the table).
LEGEND = {
    LOADS: "the server entry imported under the installed core",
    FAILED: "the server entry does not import (see the details below)",
    CLIENT_ONLY: "no server entry — a client-only bundle, covered by the code scans",
    MISSING: "not installed in the profile",
    UNAVAILABLE: "not verified yet — run the verify command",
}

#: The probe. Kept as one self-contained ESM script; the plugin name travels in an
#: environment variable so nothing has to be quoted into the source.
#:
#: It does three things, because "it imported", "it does something" and "what it
#: does still works" are different answers. First it imports the entry — the
#: failures declarations miss. Then, when the module exports an ``apply`` (a cordis
#: plugin), it CALLS it against a recording context: every service the plugin
#: reaches for is a proxy that records the method name, and every
#: ``webServer.register({path, handler})`` is harvested as a route. That is what
#: turns "no import error" into "registers this exact surface", and what lets the
#: live probe prove the surface is really served.
#:
#: Finally — and this is the part that catches a *partial* failure — it CALLS each
#: harvested route handler once, with a synthetic GET request and a recording
#: response, capturing everything the handler logs while it runs. ``apply()`` only
#: registers a closure; a free variable that was deleted in a refactor
#: (``SOME_CACHE is not defined``), a renamed service method, a bad import path
#: inside the handler all stay invisible until a request arrives, and a handler
#: with its own ``try/catch`` swallows the failure and answers ``200 []``. Nothing
#: static and no live GET can see that; running the handler and reading what it
#: logged can.
#:
#: The recording context is a stub, so it is evidence about the plugin's code, not
#: a simulation of the deployment: a service the plugin needs but the deployment
#: lacks cannot be seen from here (the plugin simply gets a stub). Module-level
#: ``ctx.effect(cb)`` callbacks ARE invoked, since that is where many plugins do
#: their registration; event handlers (``ctx.on``) are not. ``ctx.get(name)`` also
#: answers with a stub, and the lookup is recorded: a plugin that branches on it
#: takes the "service is present" branch here, which a deployment providing no such
#: service would not take, so the report has to be able to say so.
#:
#: Calling handlers has one guard: a route whose path names a mutation (delete,
#: save, reset, …) is recorded but not called, because the probe only ever sends a
#: bare GET. The skip is reported, never silent.
PROBE_SCRIPT = """
const name = process.env.DSH_PROBE_PLUGIN;
const invokeHandlers = process.env.DSH_PROBE_HANDLERS !== "0";
const started = Date.now();
const emit = (payload) => process.stdout.write("\\n" + "__DSH_PROBE__"
  + JSON.stringify(payload) + "\\n");

const routes = [];
const calls = [];
const notes = [];
const gets = [];
const handlerQueue = [];
const handlers = [];
const seenHandlerPaths = new Set();

//: A route whose path says "this call changes something" is recorded but never
//: called: the probe only sends a bare GET, and firing a mutating endpoint blind
//: is not worth the verdict. Each skip is reported in ``notes``.
const MUTATING = /(^|[/_.-])(delete|remove|reset|clear|purge|drop|wipe|truncate|destroy|overwrite|revoke|uninstall|kill|stop|cancel|save|write|update|create|install|edit|rename|move|upload|toggle|enable|disable)([/_.-]|$)/i;

//: How many handlers one plugin may have called; a plugin with a hundred routes
//: does not need all of them fired to prove the first one is broken.
const HANDLER_BUDGET = 12;

//: How long one handler may run. A handler still waiting when the window closes
//: is a lead, not a proof, so it is reported as such.
const HANDLER_TIMEOUT = 1500;

const HANDLER_KEYS = ["handler", "handle", "callback", "listener", "fn"];

function pickHandler(route) {
  if (!route || typeof route !== "object") return null;
  for (const key of HANDLER_KEYS) {
    if (typeof route[key] === "function") return route[key];
  }
  // Some plugins pass a method map instead of one function.
  if (route.handler && typeof route.handler === "object") {
    for (const key of Object.keys(route.handler)) {
      if (typeof route.handler[key] === "function") return route.handler[key];
    }
  }
  return null;
}

function syntheticRequest(path) {
  //: Just enough of IncomingMessage for a handler to read the method, the url
  //: and the headers, for one that attaches stream listeners not to throw, and
  //: for one that consumes the body with ``for await`` to see an empty one.
  return {
    method: "GET",
    url: path,
    originalUrl: path,
    httpVersion: "1.1",
    headers: { host: "127.0.0.1", "user-agent": "dsh-upgrade handler probe" },
    socket: { remoteAddress: "127.0.0.1", remotePort: 0 },
    [Symbol.asyncIterator]() {
      return { next() { return Promise.resolve({ done: true, value: undefined }); } };
    },
    on() { return this; },
    once() { return this; },
    off() { return this; },
    removeListener() { return this; },
    pause() { return this; },
    resume() { return this; },
    read() { return null; },
    setEncoding() { return this; },
    destroy() {},
  };
}

function syntheticResponse() {
  //: Just enough of ServerResponse to let the handler own the response lifecycle
  //: the way its type declares, while the probe records what it produced.
  const response = {
    statusCode: 200,
    statusMessage: "",
    headersSent: false,
    finished: false,
    writableEnded: false,
    body: "",
    headers: Object.create(null),
    setHeader(key, value) { response.headers[String(key).toLowerCase()] = value; return response; },
    getHeader(key) { return response.headers[String(key).toLowerCase()]; },
    hasHeader(key) { return String(key).toLowerCase() in response.headers; },
    removeHeader(key) { delete response.headers[String(key).toLowerCase()]; },
    writeHead(status, extra) {
      response.statusCode = Number(status) || response.statusCode;
      response.headersSent = true;
      if (extra && typeof extra === "object") {
        for (const key of Object.keys(extra)) response.setHeader(key, extra[key]);
      }
      return response;
    },
    write(chunk) {
      if (chunk !== undefined && chunk !== null) response.body += String(chunk);
      return true;
    },
    end(chunk) {
      if (chunk !== undefined && chunk !== null) response.body += String(chunk);
      response.finished = true;
      response.writableEnded = true;
      response.headersSent = true;
      return response;
    },
    flushHeaders() {},
    on() { return response; },
    once() { return response; },
    off() { return response; },
    removeListener() { return response; },
    emit() { return true; },
  };
  return response;
}

function harvestRoutes(args, serviceName) {
  const batch = Array.isArray(args[0]) ? args[0] : [args[0]];
  for (const route of batch) {
    if (!route || typeof route !== "object" || typeof route.path !== "string") continue;
    const handler = pickHandler(route);
    routes.push({
      path: route.path,
      kind: typeof route.kind === "string" ? route.kind : null,
      service: serviceName,
      handler: Boolean(handler)
    });
    if (!handler || !invokeHandlers) continue;
    if (seenHandlerPaths.has(route.path)) continue;
    seenHandlerPaths.add(route.path);
    if (MUTATING.test(route.path)) {
      notes.push("handler not called: " + route.path
        + " (the path names a mutation and the probe only sends GET)");
      continue;
    }
    if (handlerQueue.length >= HANDLER_BUDGET) {
      notes.push("handler not called: " + route.path + " (handler budget reached)");
      continue;
    }
    handlerQueue.push({ path: route.path, handler });
  }
}

async function invokeHandler(path, handler) {
  //: One call, fully instrumented: what it threw, what it logged, what it
  //: answered. The console is wrapped only for the duration of the call.
  const record = { path, error: null, logs: [], status: null, body: "",
                   timedOut: false, ms: 0 };
  handlers.push(record);
  const lines = [];
  const levels = ["log", "info", "warn", "error", "debug", "trace"];
  const original = {};
  for (const level of levels) {
    original[level] = console[level];
    console[level] = (...args) => {
      const text = args.map((value) => {
        if (value instanceof Error) return String(value.stack || value);
        if (typeof value === "string") return value;
        try { return JSON.stringify(value); } catch { return String(value); }
      }).join(" ");
      lines.push(level + ": " + text.split("\\n")[0]);
    };
  }
  const res = syntheticResponse();
  const begin = Date.now();
  let timer = null;
  let expired = false;
  try {
    const result = handler(syntheticRequest(path), res, () => {});
    await Promise.race([
      Promise.resolve(result),
      new Promise((resolve) => {
        timer = setTimeout(() => { expired = true; resolve(); }, HANDLER_TIMEOUT);
      }),
    ]);
  } catch (error) {
    record.error = String((error && (error.stack || error)) || "unknown error").split("\\n")[0];
  } finally {
    if (timer) clearTimeout(timer);
    for (const level of levels) console[level] = original[level];
  }
  record.logs = lines.slice(0, 8);
  record.status = res.statusCode;
  record.body = res.body.slice(0, 200);
  // A handler that answered and then held the stream open (SSE) is not stalled.
  record.timedOut = expired && !res.finished;
  record.ms = Date.now() - begin;
  return record;
}

function serviceProxy(label) {
  //: A recording stub that is callable AND self-returning. It has to be, or the
  //: probe would blame the plugin for its own gaps: a handler that walks
  //: ``ctx.get("credentials").resolve(...)`` — a real cordis API — must keep
  //: running against the stub instead of dying on "ctx.get is not a function",
  //: and a value the stub hands back must chain like the real one
  //: (``resolved.value.trim()``), not stop at "trim is not a function".
  const cache = Object.create(null);
  return new Proxy(function () {}, {
    get(_t, prop) {
      if (typeof prop === "symbol" || PROTOCOL_KEYS.has(String(prop))) return undefined;
      const key = String(prop);
      if (!(key in cache)) cache[key] = serviceProxy(label + "." + key);
      return cache[key];
    },
    apply(_t, _this, args) {
      calls.push(label + "()");
      if (label.endsWith(".register")) {
        harvestRoutes(args, label.slice(0, -".register".length));
      }
      return serviceProxy(label + "()");
    }
  });
}

//: Properties the JS runtime itself probes; answering them would invite a proxy
//: to be coerced or awaited forever instead of simply being absent.
const PROTOCOL_KEYS = new Set(["then", "inspect", "toJSON", "valueOf", "toString",
  "constructor", "prototype", "length", "name", "caller", "arguments"]);

const context = Object.create(null);
const ctx = new Proxy(context, {
  get(_t, prop) {
    if (typeof prop === "symbol" || prop === "then") return undefined;
    const key = String(prop);
    if (key in context) return context[key];
    if (key === "effect") {
      context[key] = (callback) => {
        calls.push("ctx.effect");
        try {
          const disposer = typeof callback === "function" ? callback() : undefined;
          return typeof disposer === "function" ? disposer : () => {};
        } catch (error) {
          notes.push("effect: " + String(error && error.message || error).split("\\n")[0]);
          return () => {};
        }
      };
      return context[key];
    }
    if (key === "inject") {
      // The standard cordis registration shape: inject(names, callback). The
      // callback body IS the registration, so it has to run.
      context[key] = (first, second) => {
        const callback = typeof first === "function" ? first : second;
        const names = Array.isArray(first) ? first.join(",") : String(first);
        calls.push("ctx.inject(" + names + ")");
        if (typeof callback === "function") {
          try {
            callback(ctx);
          } catch (error) {
            notes.push("inject: " + String(error && error.message || error).split("\\n")[0]);
          }
        }
        return () => {};
      };
      return context[key];
    }
    if (key === "on" || key === "once" || key === "emit" || key === "set"
        || key === "provide" || key === "plugin" || key === "isolate") {
      context[key] = (...args) => {
        calls.push("ctx." + key + (typeof args[0] === "string" ? "(" + args[0] + ")" : ""));
        return () => {};
      };
      return context[key];
    }
    if (key === "get") {
      //: Cordis resolves a service by name and answers ``undefined`` when nothing
      //: provides it. The recording context cannot afford that (see
      //: ``serviceProxy``): a plugin guarding a block with ``if (ctx.get("x"))``
      //: would take the other branch here than in production. The name is
      //: recorded instead, so the report can point at the branch that ran rather
      //: than leave the reader with a bare error thrown from inside it.
      context[key] = (service) => {
        const serviceName = String(service);
        gets.push(serviceName);
        calls.push("ctx.get(" + serviceName + ")");
        if (serviceName in context) return context[serviceName];
        return serviceProxy(serviceName);
      };
      return context[key];
    }
    context[key] = serviceProxy(key);
    return context[key];
  }
});

try {
  const mod = await import(name);
  const keys = Object.keys(mod || {}).sort().slice(0, 24);
  const candidate = mod.apply
    ?? (typeof mod.default === "function" ? mod.default : mod.default?.apply);
  let applyError = null;
  let applied = false;
  if (typeof candidate === "function") {
    applied = true;
    try {
      // The second argument is the row's config. An entry installed without
      // instance config gets none from the loader, and plugins branch on that
      // (mcp-lazy: `if (config === undefined) return`), so an empty object is
      // passed to model "an entry with an empty config block".
      await candidate(ctx, {});
    } catch (error) {
      applyError = String(error && error.message || error).split("\\n")[0];
    }
  }
  // Only after apply() has run: invoking a handler during registration would
  // re-enter the plugin in the middle of its own setup. Sequential, so each
  // handler's console output belongs unambiguously to that handler.
  if (invokeHandlers) {
    for (const item of handlerQueue) {
      try {
        await invokeHandler(item.path, item.handler);
      } catch (error) {
        notes.push("handler probe: " + String(error && error.message || error).split("\\n")[0]);
      }
    }
  }
  emit({
    ok: true,
    ms: Date.now() - started,
    keys,
    inject: Array.isArray(mod.inject) ? mod.inject.map(String) : [],
    applied,
    applyError,
    routes,
    handlers,
    calls: [...new Set(calls)].sort().slice(0, 24),
    gets: [...new Set(gets)].sort().slice(0, 32),
    notes
  });
} catch (error) {
  const message = (error && (error.message || String(error))) || "unknown error";
  emit({ ok: false, ms: Date.now() - started, error: String(message).split("\\n")[0] });
}
"""


@dataclass
class Probe:
    """The verdict for one plugin."""

    name: str
    status: str
    detail: str = ""
    ms: int = 0
    entry: str | None = None
    #: The specifier actually probed — a row may mount a subpath (``pkg/tools``).
    module: str | None = None
    exports: list[str] = field(default_factory=list)
    #: What ``apply()`` registered, harvested by the recording context.
    routes: list[dict] = field(default_factory=list)
    #: False when the module exports no ``apply`` at all (a declarative bundle).
    applied: bool = False
    #: The plugin's declared cordis ``inject`` list.
    injects: list[str] = field(default_factory=list)
    #: Services and methods ``apply()`` actually reached for.
    calls: list[str] = field(default_factory=list)
    #: Service names looked up with ``ctx.get(name)``. The recording context
    #: answers every name, so the report needs these to say which lookup entered
    #: the "service is present" branch a real host may never take.
    lookups: list[str] = field(default_factory=list)
    apply_error: str | None = None
    #: HTTP status per registered path, filled in by the live probe.
    live: dict[str, int] = field(default_factory=dict)
    #: What each registered route handler did on its first call: ``path``,
    #: ``error``, ``logs``, ``status``, ``body``, ``timedOut``, ``ms``. Empty when
    #: the plugin registers no route, or when the probe ran without handler calls.
    handlers: list[dict] = field(default_factory=list)
    #: Anything the probe wanted to say without calling it a failure: an
    #: ``effect``/``inject`` callback that threw, a handler it deliberately did
    #: not call, a route it skipped, a service lookup no deployment provides.
    #: Raw and stable; :func:`note_family` classifies each entry and
    #: :func:`note_line` renders it for a reader.
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS.get(self.status, "?")

    @property
    def route_paths(self) -> list[str]:
        return [str(route.get("path")) for route in self.routes if route.get("path")]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "ms": self.ms,
            "entry": self.entry,
            "module": self.module,
            "exports": self.exports,
            "routes": self.routes,
            "applied": self.applied,
            "injects": self.injects,
            "calls": self.calls,
            "lookups": self.lookups,
            "apply_error": self.apply_error,
            "live": self.live,
            "handlers": self.handlers,
            "notes": self.notes,
        }


def node_binary() -> str | None:
    """The ``node`` that runs the plugins (the one DSH itself runs on)."""
    return shutil.which("node")


def server_entry(plugin_dir: Path) -> str | None:
    """What Node resolves for the bare package name, or None for a client-only one.

    A package whose ``exports`` map has no ``"."`` subpath cannot be imported by
    name at all — that is not a failure the probe should report, it is a package
    that only ships a client bundle.
    """
    directory = Path(plugin_dir)
    manifest = read_json(directory / "package.json")
    if not isinstance(manifest, dict):
        return None
    exports = manifest.get("exports")
    if isinstance(exports, str) and exports.strip():
        return exports
    if isinstance(exports, dict):
        return "." if "." in exports else None
    main = manifest.get("main")
    if isinstance(main, str) and main.strip():
        return main
    if (directory / "index.js").is_file():
        return "index.js"
    return None


def probe_one(name: str, profile_dir: Path, *, node: str | None = None,
              entry: str | None = ".", module: str | None = None,
              timeout: int = PROBE_TIMEOUT, handlers: bool = True) -> Probe:
    """Import one plugin's entry in its own Node process.

    ``module`` is the specifier to import; it defaults to the package name, but a
    loader row may mount a subpath instead (``sample-subpath/dsh``), and that subpath
    is the entry that actually carries ``apply()``.

    ``handlers`` also CALLS every route handler ``apply()`` registered, once, with
    a synthetic GET — the only way to see a failure that lives inside the handler
    closure rather than in ``apply()`` itself.
    """
    specifier = module or name
    if node is None:
        node = node_binary()
    if node is None:
        return Probe(name, UNAVAILABLE, "node not found in PATH", module=specifier)
    if entry is None and module is None:
        return Probe(name, CLIENT_ONLY, "no server entry (client-only bundle)")

    started = time.monotonic()
    environment = {**os.environ, "DSH_PROBE_PLUGIN": specifier,
                   "DSH_PROBE_HANDLERS": "1" if handlers else "0"}
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-e", PROBE_SCRIPT],
            cwd=str(profile_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return Probe(name, FAILED, f"timed out after {timeout}s", entry=entry, module=specifier)
    except OSError as error:
        return Probe(name, UNAVAILABLE, f"cannot run node: {error}", module=specifier)

    payload = _parse_probe(result.stdout)
    elapsed = int((time.monotonic() - started) * 1000)
    if payload is None:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return Probe(name, FAILED, detail[-1] if detail else "the probe produced no verdict",
                     ms=elapsed, entry=entry, module=specifier)
    if payload.get("ok"):
        return Probe(
            name, LOADS,
            ms=int(payload.get("ms") or elapsed),
            entry=entry,
            module=specifier,
            exports=[str(item) for item in payload.get("keys") or []],
            routes=[route for route in payload.get("routes") or [] if isinstance(route, dict)],
            applied=bool(payload.get("applied")),
            injects=[str(item) for item in payload.get("inject") or []],
            calls=[str(item) for item in payload.get("calls") or []],
            lookups=[str(item) for item in payload.get("gets") or []],
            apply_error=payload.get("apply_error"),
            handlers=[record for record in payload.get("handlers") or []
                      if isinstance(record, dict)],
            notes=[str(item) for item in payload.get("notes") or []],
        )
    return Probe(name, FAILED, str(payload.get("error") or "import failed"),
                 ms=int(payload.get("ms") or elapsed), entry=entry, module=specifier)


def _parse_probe(stdout: str) -> dict | None:
    """The last probe line of the output (a plugin may print at import time)."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(PROBE_MARKER):
            try:
                payload = json.loads(line[len(PROBE_MARKER):])
            except ValueError:
                return None
            return payload if isinstance(payload, dict) else None
    return None


def mounted_specifiers(plugin, effective) -> list[str]:
    """Every module specifier the loader mounts for this plugin, in row order.

    A plugin is not one module: it may mount a package, several of its subpaths and
    the bare name together, with an empty ``apply()`` on the entry while the real
    work sits in the subpaths; another mounts only a subpath. Probing just the bare
    name would report the first as a no-op and the second as "declarative".
    """
    return [row.name for row in effective.enabled()
            if row.name == plugin.name or row.name.startswith(plugin.name + "/")]


def merge_probe(existing: Probe | None, new: Probe) -> Probe:
    """Fold the probe of one mounted module into the plugin's verdict."""
    if existing is None:
        return new
    failed = existing.status == FAILED or new.status == FAILED
    status = FAILED if failed else (LOADS if LOADS in (existing.status, new.status)
                                    else existing.status)
    detail = existing.detail
    if new.status == FAILED and existing.status != FAILED:
        detail = f"{new.module}: {new.detail}" if new.module else new.detail
    routes = list(existing.routes)
    seen = {(route.get("path"), route.get("service")) for route in routes}
    for route in new.routes:
        key = (route.get("path"), route.get("service"))
        if key not in seen:
            seen.add(key)
            routes.append(route)
    modules = [part for part in (existing.module, new.module) if part]
    unique_modules = list(dict.fromkeys(modules))
    handlers = list(existing.handlers)
    seen_paths = {str(record.get("path")) for record in handlers}
    for record in new.handlers:
        if str(record.get("path")) not in seen_paths:
            seen_paths.add(str(record.get("path")))
            handlers.append(record)
    return Probe(
        name=existing.name,
        status=status,
        detail=detail,
        ms=existing.ms + new.ms,
        entry=existing.entry or new.entry,
        module=", ".join(unique_modules) if unique_modules else None,
        exports=sorted(set(existing.exports) | set(new.exports)),
        routes=routes,
        applied=existing.applied or new.applied,
        injects=sorted(set(existing.injects) | set(new.injects)),
        calls=sorted(set(existing.calls) | set(new.calls)),
        lookups=sorted(set(existing.lookups) | set(new.lookups)),
        apply_error=existing.apply_error or new.apply_error,
        live={**new.live, **existing.live},
        handlers=handlers,
        notes=sorted(set(existing.notes) | set(new.notes)),
    )


#: ``ctx.provide(name)`` exactly as the recording context writes it into ``calls``.
_PROVIDE_RE = re.compile(r"^ctx\.provide\(([^)]+)\)$")


def provided_services(results: dict[str, Probe]) -> set[str]:
    """Every service name the probed plugins registered for themselves."""
    provided: set[str] = set()
    for probe in results.values():
        for call in probe.calls:
            match = _PROVIDE_RE.match(str(call))
            if match:
                provided.add(match.group(1))
    return provided


def core_service_names(names: set[str], install: Path | None = None) -> set[str]:
    """Which of ``names`` the installed core spells anywhere in its own bytes.

    Cordis registers a service under its literal name, so a name the core never
    spells cannot be a core service. This is only used to keep an absent-service
    note honest, and it errs towards silence: a name found here is left alone, and
    a core that cannot be located yields every name (nothing claimed). The scan
    reads the core's generated declarations and code once per probe run, and only
    when there is a name to look up.
    """
    if not names:
        return set()
    modules = host_modules_dir(core_install_dir() if install is None else install)
    if modules is None:
        return set(names)
    found: set[str] = set()
    for pattern in ("*/lib/**/*.d.ts", "*/lib/**/*.js"):
        for path in modules.glob("@deepseek-ai/" + pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in names - found:
                if name in text:
                    found.add(name)
            if found == names:
                return found
    return found


def _annotate_service_lookups(results: dict[str, Probe]) -> None:
    """Name the ``ctx.get`` lookups that entered the "service is present" branch.

    The recording context answers every name with a working stub, so a plugin
    guarding a block with ``if (ctx.get("x"))`` runs that block even when nothing
    in the deployment provides ``x``. When something inside the block then throws,
    the bare error reads like the plugin's own bug; this ties the two facts
    together. The note is emitted only where it explains something — a probe whose
    callback threw — and only for a name no probed plugin provides and the
    installed core never spells, which is what makes ``undefined`` the answer a
    real host would give.
    """
    looked_up = {name for probe in results.values() for name in probe.lookups}
    if not looked_up:
        return
    absent = looked_up - provided_services(results) - core_service_names(looked_up)
    if not absent:
        return
    for probe in results.values():
        if not any(note_family(note)[0] == STUB_CAUSE for note in probe.notes):
            continue
        for name in sorted(set(probe.lookups) & absent):
            note = (f"service lookup: ctx.get('{name}') — no probed plugin provides it and "
                    f"the installed core never names it, so a real host resolves it to "
                    f"undefined; the recording context answered it instead, and the branch "
                    f"that used it may not run there at all")
            if note not in probe.notes:
                probe.notes.append(note)
        probe.notes.sort()


def probe_profile(profile, *, node: str | None = None, jobs: int = DEFAULT_JOBS,
                  timeout: int = PROBE_TIMEOUT, log=None,
                  handlers: bool = True) -> dict[str, Probe]:
    """Probe every INSTALLED plugin of the profile, in parallel."""
    targets: list[tuple[str, str, str | None]] = []
    results: dict[str, Probe] = {}
    effective = effects_mod.resolve(profile)
    for plugin in profile.plugins:
        if not plugin.installed or not plugin.directory:
            results[plugin.name] = Probe(plugin.name, MISSING, "not installed in the profile")
            continue
        specifiers = mounted_specifiers(plugin, effective)
        entry = server_entry(Path(plugin.directory))
        if not specifiers:
            if entry is None:
                results[plugin.name] = Probe(plugin.name, CLIENT_ONLY,
                                             "no server entry (client-only bundle)")
                continue
            specifiers = [plugin.name]
        for specifier in specifiers:
            targets.append((plugin.name, specifier, entry))
    if not targets:
        return results
    if node is None:
        node = node_binary()
    if node is None:
        for name, _, _ in targets:
            results[name] = Probe(name, UNAVAILABLE, "node not found in PATH")
        return results

    if log is not None:
        log(f"  probing {len(targets)} installed plugin(s) with {node}")

    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(targets)))) as pool:
        futures = {
            pool.submit(probe_one, name, Path(profile.directory), node=node,
                        entry=entry, module=module, timeout=timeout,
                        handlers=handlers): (name, module)
            for name, module, entry in targets
        }
        for future, (name, module) in futures.items():
            try:
                probe = future.result()
            except Exception as error:  # noqa: BLE001 - never let one plugin break the report
                probe = Probe(name, FAILED, f"probe crashed: {error}", module=module)
            results[name] = merge_probe(results.get(name), probe)
    _annotate_service_lookups(results)
    return results


# --------------------------------------------------------------------------- #
# Cache: the probe is cheap but not free, and it executes plugin code, so it runs
# only when the thing it measured has changed.
# --------------------------------------------------------------------------- #

def cache_path(profile_name: str) -> Path:
    return state_dir() / f"verified-{profile_name}.json"


def fingerprint(profile, core: str | None) -> str:
    """What the cached verdicts are valid for: the probe schema, core, plugin copies."""
    parts = [f"probe={PROBE_SCHEMA}", f"core={core or '?'}"]
    for plugin in sorted(profile.plugins, key=lambda item: item.name):
        if not plugin.installed or not plugin.directory:
            parts.append(f"{plugin.name}=missing")
            continue
        stamp = ""
        try:
            stamp = str((Path(plugin.directory) / "package.json").stat().st_mtime_ns)
        except OSError:
            stamp = "?"
        parts.append(f"{plugin.name}@{plugin.version}#{stamp}")
    return "|".join(parts)


def load_cache(profile, core: str | None) -> dict[str, Probe]:
    """Cached verdicts, or an empty dict when the profile has changed since."""
    payload = read_json(cache_path(profile.name))
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint(profile, core):
        return {}
    results: dict[str, Probe] = {}
    for item in payload.get("plugins") or []:
        probe = probe_from_payload(item)
        if probe is not None:
            results[probe.name] = probe
    return results


def probe_from_payload(item) -> Probe | None:
    """One :class:`Probe` from a saved ``plugins`` entry (None when unusable)."""
    if not isinstance(item, dict) or not item.get("name"):
        return None
    return Probe(
        name=str(item["name"]),
        status=str(item.get("status") or UNAVAILABLE),
        detail=str(item.get("detail") or ""),
        ms=int(item.get("ms") or 0),
        entry=item.get("entry"),
        module=item.get("module"),
        exports=[str(value) for value in item.get("exports") or []],
        routes=[route for route in item.get("routes") or [] if isinstance(route, dict)],
        applied=bool(item.get("applied")),
        injects=[str(value) for value in item.get("injects") or []],
        calls=[str(value) for value in item.get("calls") or []],
        lookups=[str(value) for value in item.get("lookups") or []],
        apply_error=item.get("apply_error"),
        live={str(key): int(value) for key, value in (item.get("live") or {}).items()},
        handlers=[record for record in item.get("handlers") or []
                  if isinstance(record, dict)],
        notes=[str(value) for value in item.get("notes") or []],
    )


def save_cache(profile, core: str | None, results: dict[str, Probe]) -> Path:
    path = cache_path(profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile": profile.name,
        "core": core,
        "fingerprint": fingerprint(profile, core),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "plugins": [results[name].to_dict() for name in sorted(results)],
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def resolve(profile, core: str | None, *, refresh: bool = False, allow_probe: bool = True,
            log=None, jobs: int = DEFAULT_JOBS, handlers: bool = True,
            save: bool = True) -> dict[str, Probe]:
    """Verdicts for the profile: from the cache, else from a fresh probe.

    ``allow_probe=False`` never executes anything (the plain ``status`` command is
    read-only); it still returns a valid cache if one exists, so the column fills in
    as soon as ``verify`` has been run once.

    ``handlers=False`` runs the probe without calling the route handlers; the
    result is then a weaker answer and is deliberately NOT cached, so a cache
    never silently loses the handler evidence a later reader would trust.
    """
    cached = load_cache(profile, core)
    if cached and not refresh:
        return cached
    if not allow_probe:
        return cached
    if node_binary() is None:
        return {plugin.name: Probe(plugin.name, UNAVAILABLE, "node not found in PATH")
                for plugin in profile.plugins}
    results = probe_profile(profile, log=log, jobs=jobs, handlers=handlers)
    if save and handlers:
        save_cache(profile, core, results)
    return results


# --------------------------------------------------------------------------- #
# Comparing two verification results
# --------------------------------------------------------------------------- #

#: The facts a comparison reads. They are the ones a saved result carries and a
#: reader of the table sees in its own columns, so "what changed" needs no other
#: input than the two results themselves.
DIFF_FIELDS = ("loads", "surface")


def load_verified(path) -> tuple[str | None, list[dict]]:
    """Read a saved verification result: ``(generated, plugin entries)``.

    Both forms the tool writes are accepted — the state file produced by
    :func:`save_cache` (an object with a ``plugins`` list) and a bare list of
    plugin entries. Raises ``ValueError`` when the file is not one of them.
    """
    payload = read_json(Path(path))
    if payload is None:
        raise ValueError(f"not readable JSON: {path}")
    if isinstance(payload, list):
        return None, [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError(f"not a verification result: {path}")
    generated = payload.get("generated") if isinstance(payload.get("generated"), str) else None
    items = [item for item in payload.get("plugins") or [] if isinstance(item, dict)]
    return generated, items


def probe_signature(probe: Probe) -> dict:
    """The comparable facts of one probe: its load label and its surface."""
    return {"loads": LABELS.get(probe.status, "?"), "surface": surface_text(probe)}


def stored_signature(item: dict) -> dict:
    """The same facts for a saved plugin entry."""
    probe = probe_from_payload(item)
    return probe_signature(probe) if probe is not None else {"loads": "?", "surface": "?"}


def diff_probes(old_items: list[dict], current: dict[str, Probe]) -> dict:
    """Differences between a saved result and the current probes.

    Only ``(loads, surface)`` per plugin is compared, and a plugin present on one
    side only is reported as added or removed: an empty ``changed`` list with a
    non-zero ``unchanged_count`` means the two results agree on every plugin they
    have in common.
    """
    old = {str(item["name"]): item for item in old_items
           if isinstance(item, dict) and item.get("name")}
    changed: list[dict] = []
    added: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for name in sorted(set(old) | set(current)):
        if name not in current:
            removed.append(name)
            continue
        if name not in old:
            added.append(name)
            continue
        before = stored_signature(old[name])
        after = probe_signature(current[name])
        fields = {
            field: {"old": before[field], "new": after[field]}
            for field in DIFF_FIELDS if before[field] != after[field]
        }
        if fields:
            changed.append({"plugin": name, "fields": fields})
        else:
            unchanged += 1
    return {"changed": changed, "added": added, "removed": removed,
            "unchanged_count": unchanged}


# --------------------------------------------------------------------------- #
# The live probe: is a surface a plugin registered really served?
# --------------------------------------------------------------------------- #

#: Where ``dsh web`` serves by default.
DEFAULT_WEB_URL = "http://127.0.0.1:3080"

#: Registered-but-unserved, i.e. the path answers 404.
NOT_SERVED = 404


def route_status(base_url: str, path: str, *, timeout: float = 3.0) -> int:
    """The HTTP status for one path, or 0 when the host cannot be reached.

    The request is a plain GET with no body. It does reach the plugin's own
    handler when the route is registered — a handler that mutates state on an
    empty request would feel that — so the live probe is always opt-in and its
    output says what it did.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    request = Request(url, method="GET", headers={"user-agent": "dsh-upgrade (live probe)"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200))
    except HTTPError as error:
        return int(error.code)
    except (URLError, OSError, ValueError):
        return 0


def host_reachable(base_url: str, *, timeout: float = 1.5) -> bool:
    """True when something HTTP answers at the base URL."""
    return route_status(base_url, "/", timeout=timeout) != 0


def live_probe(probes: dict[str, Probe], *, base_url: str | None = None,
               timeout: float = 3.0, log=None) -> tuple[bool, dict[str, dict[str, int]]]:
    """Check every harvested route against the running DSH.

    Returns ``(reachable, verdicts)``: a path that answers anything except 404 is
    served, which is proof that the plugin's row applied in the live deployment —
    something no import can show.
    """
    url = (base_url or DEFAULT_WEB_URL).strip() or DEFAULT_WEB_URL
    targets = {name: probe.route_paths for name, probe in probes.items() if probe.route_paths}
    if not targets:
        return False, {}
    if not host_reachable(url, timeout=min(timeout, 1.5)):
        return False, {}
    if log is not None:
        log(f"  probing {sum(len(paths) for paths in targets.values())} registered "
            f"route(s) on {url}")
    verdicts: dict[str, dict[str, int]] = {}
    for name, paths in sorted(targets.items()):
        verdicts[name] = {path: route_status(url, path, timeout=timeout) for path in paths}
    return True, verdicts


def apply_live(probes: dict[str, Probe], verdicts: dict[str, dict[str, int]]) -> None:
    """Attach live verdicts to the probes, in place."""
    for name, statuses in verdicts.items():
        probe = probes.get(name)
        if probe is not None:
            probe.live = dict(statuses)


# --------------------------------------------------------------------------- #
# What the ``surface`` column means
# --------------------------------------------------------------------------- #

#: Text shown when the plugin's server half registered nothing to observe.
NO_APPLY = "declarative"
NO_OP = "no-op"
APPLY_FAILED = "apply!"
SHADOWED = "shadowed"
#: The lead form: a code reference to a disabled row, with no replacement found.
SHADOW_SUSPECT = "shadowed?"
#: RPC calls this core no longer serves — checked against its own TYPERT faces.
WIRE_DEAD = "wire:404"
#: A registered route handler whose first call threw, or logged a failure. This is
#: the "partial functionality" case: the plugin loads, its apply() runs, the route
#: is registered — and the request that uses it is answered by a broken closure.
HANDLER_FAILED = "handler!"
#: The weaker form: a handler that warned, or was still running when the probe
#: window closed. Evidence, not proof.
HANDLER_SUSPECT = "handler?"

#: Console lines that name a failure. Kept as patterns rather than a word list
#: because the useful evidence is usually a JS failure class or an errno, and
#: matching on the class is what makes this robust across wording.
_FAILURE_PATTERNS = (
    # ReferenceError, TypeError, SyntaxError, ... but not "ErrorHandling".
    re.compile(r"\b[A-Za-z]*Error\b"),
    re.compile(r"\bis not (?:a function|defined|iterable|an object|a constructor)\b"),
    re.compile(r"\b(?:undefined|null) is not\b"),
    re.compile(r"\b(?:failed|failure|threw|exception|unhandled|traceback|"
               r"crashed|rejected)\b", re.I),
    re.compile(r"\bE(?:NOENT|ACCES|ROFS|PERM|EXIST|ISDIR|BUSY|CONNREFUSED|CONNRESET)\b"),
    # A stack frame line, which only a thrown error prints.
    re.compile(r"^\s*at\s+\S+.*:\d+:\d+"),
)

#: Failures the recording stub CANNOT have caused. An undeclared identifier is
#: undeclared under any context, so ``ReferenceError: X is not defined`` is proof
#: about the plugin (a free variable removed in a refactor, still read by a
#: handler — the shape this probe exists to catch).
_STUB_IMMUNE = (
    re.compile(r"\bReferenceError\b"),
    re.compile(r"\bis not defined\b"),
)

#: Failures the recording stub CAN produce on its own: a proxy is not a string, a
#: synthetic request has no body, an unknown service is not the real one. A real
#: breakage can look identical, so these are reported as a LEAD with the evidence
#: — never as a verdict — and the reader decides.
_STUB_INDUCIBLE = (
    re.compile(r"\bis not a function\b"),
    re.compile(r"\bis not (?:async )?iterable\b"),
    re.compile(r"\bCannot read propert"),
    re.compile(r"\bCannot destructure\b"),
    re.compile(r"\bundefined is not\b"),
    #: Node's own argument validation, reached when a stub proxy is handed to a
    #: ``path``/``fs``/``Buffer`` call that wanted a string: a proxy is not one.
    #: The message names the received value, and a proxy's ``name`` is
    #: deliberately not answered (``PROTOCOL_KEYS``), which is why the reader
    #: sees "Received function undefined" rather than a value.
    re.compile(r"\bERR_INVALID_ARG_TYPE\b"),
    re.compile(r"\bmust be of type\b"),
    re.compile(r"\bReceived (?:function|an instance of|object|undefined|number)\b"),
)


def _clip(text: str, limit: int = 160) -> str:
    """One log line, short enough to print next to a table row."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _looks_like_failure(text: str) -> bool:
    return any(pattern.search(text) for pattern in _FAILURE_PATTERNS)


def _stub_immune(text: str) -> bool:
    """True when this failure cannot be the recording stub's own doing."""
    return any(pattern.search(text) for pattern in _STUB_IMMUNE)


def _stub_inducible(text: str) -> bool:
    """True when the recording stub could have produced this failure itself."""
    return any(pattern.search(text) for pattern in _STUB_INDUCIBLE)


def _stub_note(text: str) -> str:
    """A reminder that the probe may be the cause, when it plausibly is."""
    return " (the recording stub can cause this)" if _stub_inducible(text) else ""


#: The kinds of probe note, for a reader who should not have to parse the sentence:
#: ``SKIPPED`` — the probe deliberately did not call something (by design, not a
#: finding); ``STUB_CAUSE`` — a plugin callback threw while the probe ran it, and
#: the recording context is a plausible cause; ``PROBE_ERROR`` — the probe itself
#: could not finish a step.
SKIPPED = "skipped"
STUB_CAUSE = "stub"
PROBE_ERROR = "probe"
UNCLASSIFIED = "other"

#: Note prefix -> (kind, what the note means). The prefix is what the probe script
#: writes; ``notes`` itself stays raw so ``--json`` keeps the probe's own words.
_NOTE_FAMILIES = (
    ("handler not called: ", SKIPPED,
     "nothing is known about this route: it is neither a pass nor a failure"),
    ("effect: ", STUB_CAUSE,
     "a callback the plugin scheduled with ctx.effect threw while the probe ran it"),
    ("inject: ", STUB_CAUSE,
     "the registration callback the plugin passed to ctx.inject threw while the probe ran it"),
    ("handler probe: ", PROBE_ERROR,
     "the probe could not run one registered handler — this says nothing about the plugin"),
    ("service lookup: ", STUB_CAUSE,
     "this is what put the failure above within the probe's reach: a branch a real "
     "host would skip"),
)


def note_family(note: str) -> tuple[str, str]:
    """The kind of one probe note, and what that kind of note means."""
    text = str(note)
    for prefix, kind, meaning in _NOTE_FAMILIES:
        if text.startswith(prefix):
            return kind, meaning
    return UNCLASSIFIED, "the probe reported this without calling it a failure"


def note_effect(note: str) -> str:
    """A note's payload with its ``family: `` marker stripped."""
    head, separator, rest = str(note).partition(": ")
    return rest if separator else head


def note_line(note: str) -> tuple[str, str]:
    """One note as ``(headline, meaning)`` — what was reported, and whether it matters.

    ``notes`` stays raw (it is also machine-readable output); this is the
    reader-facing rendering. For a callback the recording stub could have broken by
    itself, the headline carries the explicit reminder, so a bare ``TypeError``
    from inside a registration callback is never mistaken for a plugin bug.
    """
    kind, meaning = note_family(note)
    text = str(note)
    if kind == SKIPPED:
        path = note_effect(text).split(" (", 1)[0]
        reason = ("the path names a mutation and the probe only sends GET"
                  if "(the path names a mutation" in text
                  else "the per-plugin handler budget was reached")
        return f"GET {path} — not called: {reason}", meaning
    if kind in (STUB_CAUSE, PROBE_ERROR):
        return f"{note_effect(text)}{_stub_note(text)}", meaning
    return text, meaning


def handler_failure(record: dict) -> str | None:
    """Proof that one handler's first call is broken, or None when there is none.

    Only stub-immune evidence counts. A ``ReferenceError`` is proof either way it
    appears: an undeclared binding is undeclared in every context, so the plugin —
    not the probe — is broken. A free variable removed in a refactor and still read
    by a handler that catches its own error and answers ``200 []`` is exactly that
    shape, and nothing else can see it.

    Everything else is deliberately a LEAD (:func:`handler_suspect`), with the
    evidence attached. A ``TypeError`` mid-chain is what a recording stub produces
    ("resolved.value.trim is not a function"); an ``ENOENT`` for a cache file that
    has not been written yet is what a handler that falls back on purpose
    produces. Both are worth a reader's eye and neither is a verdict, and a tool
    that calls them failures would cry wolf on a working deployment.
    """
    error = record.get("error")
    if error and _stub_immune(str(error)):
        return f"the first call threw: {_clip(str(error))}"
    for line in record.get("logs") or []:
        level, separator, text = str(line).partition(": ")
        if separator and _stub_immune(text):
            return f"the first call logged a failure: {_clip(text)}"
    return None


def handler_suspect(record: dict) -> str | None:
    """A weaker signal from the same call — reported, never called a verdict."""
    if handler_failure(record):
        return None
    error = record.get("error")
    if error:
        return f"the first call threw: {_clip(str(error))}{_stub_note(str(error))}"
    if record.get("timedOut"):
        return "the first call did not finish within the probe window"
    for line in record.get("logs") or []:
        level, separator, text = str(line).partition(": ")
        if not separator:
            continue
        if level == "warn":
            return f"the first call logged a warning: {_clip(text)}"
        if level == "error":
            return f"the first call logged an error: {_clip(text)}{_stub_note(text)}"
        if _looks_like_failure(text):
            return (f"the first call logged something worth reading: "
                    f"{_clip(text)}{_stub_note(text)}")
    return None


def handler_failures(probe: Probe) -> list[tuple[str, dict]]:
    """Every handler of this probe whose first call failed, with the reason."""
    found = []
    for record in probe.handlers:
        reason = handler_failure(record)
        if reason:
            found.append((reason, record))
    return found


def handler_leads(probe: Probe) -> list[tuple[str, dict]]:
    """Every handler that only produced a lead."""
    found = []
    for record in probe.handlers:
        reason = handler_suspect(record)
        if reason:
            found.append((reason, record))
    return found


def runtime_verified(probe: Probe) -> bool:
    """Did the probe prove this copy WORKS on the core it was measured against?

    The whole chain has to hold: the server entry imported, ``apply()`` did not
    throw, and every route handler it registered answered its first call. A probe
    that never ran (``unavailable``), a plugin with no server half (``client``) and a
    plugin that is not installed (``missing``) prove nothing and are not verified.

    This is a statement about one core version — the one the probe ran against. A
    verdict from an older cache does not survive a core change: it is stored under a
    fingerprint that includes the core, so the caller only ever sees verdicts taken
    against the core it asked about.
    """
    if probe.status != LOADS:
        return False
    if probe.apply_error:
        return False
    return not handler_failures(probe)


def verified_names(profile, core: str | None) -> set[str]:
    """Names whose CACHED verdict proves they work on ``core``.

    Reads the cache only — no plugin code is executed. Stale verdicts are dropped by
    the fingerprint check inside :func:`load_cache`, so a caller may use the result
    without re-checking the core version itself.
    """
    return {name for name, probe in load_cache(profile, core).items()
            if runtime_verified(probe)}


def surface_text(probe: Probe, *, shadowed: bool = False, suspect: bool = False,
                 dead_wire: int = 0, client: bool = False) -> str:
    """The ``surface`` cell: what this plugin actually puts into the deployment.

    ``loads`` says the code imported. This says what it does: how many surfaces
    ``apply()`` registered, whether the live host serves them, or — the cases the
    column exists for — that the component its browser half draws into is
    switched off, that its runtime calls address an endpoint this core does not
    serve, or that a route it registered is broken the moment it is called.

    ``client`` marks a plugin that ships a browser bundle. A plugin whose server
    half is empty is not idly "no-op" when all of its behaviour lives in that
    bundle (``dsh-theme-mineradio`` is literally an empty ``apply()`` plus a theme
    client half), and saying so would send the reader looking for a bug.
    """
    if probe.status == MISSING:
        return "—"
    if probe.status == UNAVAILABLE:
        return "?"
    if probe.status == FAILED:
        return "no"
    # A shadowed surface outranks a working server half on purpose: the column is
    # one cell, and "part of this plugin provably cannot work" is the fact that
    # must not be missed. The section below the table carries the rest.
    if shadowed:
        return SHADOWED
    # A dead RPC endpoint is equally provable — the core's own wire declaration
    # does not contain it — so it outranks every "it registered something" answer.
    if dead_wire:
        return WIRE_DEAD
    # A handler that fails on its first call is the same kind of fact: the surface
    # exists and is broken. It outranks "routes:N", which only says it registered.
    if handler_failures(probe):
        return HANDLER_FAILED
    if suspect:
        return SHADOW_SUSPECT
    if handler_leads(probe):
        return HANDLER_SUSPECT
    if probe.status == CLIENT_ONLY:
        return "client"
    if probe.apply_error:
        return APPLY_FAILED
    if probe.live:
        served = sum(1 for status in probe.live.values() if status != NOT_SERVED)
        if served:
            return f"live:{served}"
        if probe.route_paths:
            return f"404:{len(probe.route_paths)}"
    if probe.route_paths:
        return f"routes:{len(probe.route_paths)}"
    if not probe.applied:
        return "client" if client else NO_APPLY
    if probe.apply_error is None and probe.applied:
        # No routes, but it did reach for something: services, hooks, events.
        touched = [call for call in probe.calls if not call.startswith("logger.")]
        if touched:
            return f"hooks:{len(touched)}"
    return "client" if client else NO_OP


SURFACE_LEGEND = {
    "client": "its behaviour is in the browser bundle; the server half is empty or absent",
    NO_APPLY: "the entry imports but exports no apply() — nothing to run",
    NO_OP: "apply() ran and registered nothing observable",
    APPLY_FAILED: "apply() threw against the recording context",
    "routes": "surfaces apply() registered (proven by code, not by the host)",
    "hooks": "apply() registered no route, but reached for services, hooks or events",
    "live": "registered surfaces the running DSH really serves — proof it applied",
    "404": "the route is registered in the code but the running DSH answers 404",
    SHADOWED: "the host component its client half draws into is disabled (see below)",
    SHADOW_SUSPECT: "a disabled row's name appears in the client half — a lead, not a verdict",
    WIRE_DEAD: "its runtime calls address wire endpoints this core does not serve",
    HANDLER_FAILED: "a route it registered fails on its first call (see below)",
    HANDLER_SUSPECT: "a route handler warned, logged or threw on its first call — a lead",
}
