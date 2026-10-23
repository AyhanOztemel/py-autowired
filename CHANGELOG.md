# Changelog

## 0.5.4 - 2026-10-21

No problem stays silently behind a `try/except`, a DEBUG-only line or a quiet
default any more: each one is a WARNING, an ERROR or a validation issue - shown
when the application configures `logging`; validation covers the graph the
`*_instance` fields reach. Many are made visible rather than prevented (see
"Known limits" in the README); "Coming in 0.6.0" lists those that will become
errors. Nothing is removed; what gets injected changes only where "Behaviour
changes" says so.

### Added

- A file that discovery cannot load yet (it imports the application module
  while that module is still starting, typically `main.py` doing
  `from app import app`) is checked the moment it loads, with the static check
  of `validate=True` (no service is constructed): a wiring problem is an ERROR
  at once instead of appearing when a field is first used; with `validate=True`
  the import of the file fails with `AutowiringValidationError` (again if
  retried). A healthy file logs INFO `Checked ... no wiring problems`.
- If the application never imports such a file, the library imports and checks
  it at the first safe moment: the first injection after the application module
  has finished loading, or `refresh()`, `activate_container()`,
  `validate_autowiring()`. (Discovery imports every project file anyway.)
- `skipped_during_discovery()`: the module names the latest discovery skipped.
- `check_skipped(container=None)`: imports and checks every skipped module and
  raises `AutowiringValidationError` for any problem, whatever `validate` was -
  for tests and startup checks.
- `validate=True` / `validate_autowiring()` name skipped files that have not
  loaded yet on a line of their own.
- New WARNINGs where a problem used to be silent or DEBUG-only: a field with no
  matching registration (once per field), a contract without an implementation
  after discovery, fields whose best candidates tie (ONE grouped WARNING at
  startup; the first registered one is used, as before), constructor type hints
  that cannot be resolved (once per class), a project module imported by a
  loader the import hook cannot patch, a file discovery could not process, a
  module whose path cannot be read, `auto_inject()` that found nothing to load
  or patch, and a class that could not be tracked for validation (also a
  validation issue).

- An independent blind review found more silent paths; each is now visible or
  fixed (each pinned by a test that failed before the change):
  - validation reports a singleton holding `Lazy[Scoped]` (Lazy keeps the first
    value), a keyed singleton capturing a scoped field, and every project module
    discovery could not import or a late import could not patch (a
    positional-only constructor parameter, first reported here, is now supported
    - see "Root fixes" below);
  - a scoped service resolved while a singleton is being created is a WARNING
    (once per pair; behaviour unchanged). All warnings raised during resolution
    are written after every creation lock is released - a log handler may take
    locks of its own;
  - a late-imported module that cannot be patched is an ERROR (was WARNING);
  - a thread waiting at a creation gate longer than
    `container.creation_wait_warning` seconds (10 by default) logs one WARNING:
    a factory waiting (e.g. `thread.join()`) for a worker that needs the very
    service being created cannot be detected as a deadlock, but no longer hangs
    silently.

### Changed

- The skip itself is logged at INFO (was DEBUG). Real import failures are
  still WARNINGs.
- `refresh()`, `activate_container()` and `auto_inject()` raise a
  `validate=True` failure of a file that finished loading in another thread
  while the library was busy - after finishing their own work.
- Validation no longer passes what the container would fail on: a constructor
  signature that cannot be read (and cannot be rebuilt, see "Root fixes") and a
  type hint that cannot be resolved (without a default) are now issues. A
  non-callable `__init__` fails as Python makes it fail (the patched
  constructor used to swallow it).

### Fixed

- A second independent blind review found more paths that could hang or stay
  silent; each is fixed or made visible (each pinned by a test that failed
  before the change):
  - two threads reading each other's `Lazy` values hung; `Lazy` now uses the
    same creation gates as singletons and scoped services (cycle check, long
    wait warning);
  - closing a scope waited for an in-flight creation without any check, and a
    long-wait warning of a thread holding locks waited behind the very hang it
    reported: waits go through the wait-for graph and warnings are written by a
    single background writer (bounded queue; dropped warnings are counted);
  - a thread waiting for the library lock (for example `refresh()` while
    `auto_inject()` imports a module that waits for that thread) now reports it
    after `_GLOBAL_WAIT_WARNING` seconds (10);
  - an import queued while the library was busy is a WARNING (objects created
    before its classes are patched get no injection);
  - with `validate=True`, every project module imported after startup is
    checked when it loads (an import with a wiring problem fails), and a
    skipped file whose later import fails raises at the first safe moment;
  - `refresh()` reports ties created by new registrations (once per new set);
  - validation follows what a `Lazy` builds (a singleton holding
    `Lazy[Transient]` whose transient needs a scoped service is a capture),
    substitutes generic type variables as the container does, and finds
    `Provider`/`Lazy` of a closed open-generic;
  - an assignment `obj.x_instance = None` with no registration warns like
    construction does; import failures keep their traceback; a disposal note
    that neither the exception nor the log can take goes to stderr; a failed
    check of a skipped module stays a validation issue (`fastapi.injected()` of
    a never-patched class and a lost Keyed metadata pass, first made visible
    here, are now fixed at the root - see "Root fixes" below);
  - a Django ASGI request cancelled again while its scope closes still waits
    for the closing and passes its error to the caller;
  - a container that cannot be weakly referenced is kept alive while the
    library holds entries for it, so its id is never reused.
- Two registrations of the same ready object (`register_instance(A, obj)` and
  `register_instance(B, obj)`) were reported as "equally good candidates"
  (startup WARNING and `validate=True`): every candidate gives the field that
  same object, so it is no ambiguity. Two separate singletons of one class still
  are.
- A method or a read-only property whose name ends in `_instance` (for example
  a `has_instance` property) was taken for an injection field: injection could
  never fill it, validation reported it. Fields are names that can receive a
  value (class attribute, annotation, assignment, slot/attrs/dataclass field,
  property with a setter). Two tests that used a read-only property as a field
  now use a settable one; their intent (a failing getter only warns; validation
  never runs a property) is unchanged.
- A plural field declared in the class body (`repo_instances = None`) received
  one object instead of the list.
- `Lazy.value` read again while it is being resolved hung on its own lock; it
  now raises `CircularDependencyError` (and never builds a second object).
- A scoped factory waiting for a worker thread that needs another scoped
  service of the same scope hung: the scope held one lock for every creation.
  Each scoped slot now has its own creation gate, with the cross-thread cycle
  check of singletons; closing a scope from inside one of its creations raises
  instead of hanging.
- `strict_registrations=True` now also rejects a second keyed or open-generic
  registration of the same key/type.
- The Flask/Django bridges close an `aclose()`-only singleton at process exit
  instead of failing with `AsyncDisposalRequiredError`; a cancelled Django ASGI
  request still closes its scoped services.
- A finalizer attribute that is not callable (`dispose = True`) no longer hides
  the real `close()`; a failing log handler no longer loses the notes of further
  disposal errors.
- Deadlock risk: the finalizer of a garbage-collected container waited for the
  library lock. The garbage collector can run it in any thread, also one that
  the lock holder is waiting for. It never waits now: when the lock is busy the
  next lock holder finishes the job. Because it can now run late, it drops only
  its own container's entries - `id()` values are reused, and a late drop must
  never remove a newer container's (a requirement of the new design, not a 0.5.3
  bug).
- A late import queued while the lock was busy was patched only at the next
  drain of the queue (a later import, `refresh()`, `activate_container()` or
  `auto_inject()`) - which might never come. It is now patched as soon as the
  lock is free.
- The late-import hook asked every other finder itself, and when none found
  the module Python asked them all again: every failing import (an optional
  dependency tried at run time, often once per request) scanned the whole
  `sys.path` twice - about +0.5 ms per request in a real application. The hook
  now first checks whether the name can be a project module at all: its
  top-level name is a root's own name, Python's own path search finds it in a
  `sys.path` entry under a root (sharing Python's directory caches - no cache
  of the library's can say "not there" for a file just created), or a
  non-standard finder (an editable install's name map) finds it. Anything
  else is left to Python at once. What remains is one look into each
  `sys.path` entry under a root per failing import.

### Root fixes

Some problems above were first only made visible. These are now fixed where
they start (none of them breaks working code: each one used to fail, or to
inject the wrong thing or nothing):

- Positional-only constructor parameters (`def __init__(self, repo: Repo, /)`)
  are supported: the container passes them by position (a skipped one gets its
  own default). They used to fail with `TypeError`.
- Type hints are read once, with their metadata: a `Keyed` parameter can no
  longer fall back to the unkeyed registration when the metadata pass fails
  while the plain one succeeds (it fails like any unresolvable hint). A
  constructor without `Annotated` hints reads them once instead of twice.
- `fastapi.injected()` of a class that discovery never patched (outside the
  project root, or created after startup) fills the `None` fields of the object
  it builds, after `__init__` returns. The class itself is not changed (modules
  outside the project are never touched), so `__init__` cannot use those fields
  yet. A field with no registration is a WARNING, as everywhere. A registered
  type whose class was never patched is not filled this way (a shared singleton
  would keep the first request's objects); its `None` fields used to stay
  `None` silently and now each gets a WARNING naming it.
- A `__signature__` that cannot be read is rebuilt from the function's code
  (`functools.wraps` wrappers included): the container passes the right
  arguments, and validation checks the real parameters - also of a
  field-injection class. Only a callable without code (a callable object used
  as `__init__`) stays "cannot be read". One WARNING per class says the
  signature was rebuilt.
- With deferred annotations (Python 3.14+), a constructor annotation naming
  something not defined yet broke `inspect.signature` itself: the container,
  validation and class patching raised a raw `NameError`. The signature is now
  read with string annotations in that case, and the hint is reported the
  usual way (validation issue; a default is used, a parameter without one fails
  to resolve). 3.10-3.13 are unchanged.
- An optional keyed dependency is injected: `Annotated[T, Keyed(k)] = None` on
  Python 3.10 (its implicit `Optional` hid the marker), and on every version
  `Optional[Annotated[T, Keyed(k)]]`, `Union[..., None]`, `... | None` and
  `Annotated[Optional[T], Keyed(k)]`. They all got `None` while the keyed
  service was registered. A `Keyed` marker in any other form (a union with
  another type, inside `list[...]` or `Callable[...]`) is never ignored
  silently: one WARNING per parameter at runtime and a validation issue, both
  naming the field and the key.
- `auto_inject(container, validate=True)` with a container that cannot be
  weakly referenced lost the `validate=True` setting silently: modules imported
  after startup were not checked. The setting is now kept (the container is
  kept alive, as for the other per-container records, so its id is never
  reused).
- When type hints cannot be resolved and a parameter with a default has a
  `Keyed` hint, a WARNING names that field and its key: the default is used even
  if the keyed service is registered (fixing this would break code that relies
  on the default: 0.6.0).

### Third review

A last blind review of the changes above found four more; each is fixed and
pinned by a test that failed before:

- A log handler that raised stopped the background warning writer for good:
  later warnings were never written. The writer now survives a failing handler
  and writes that warning to stderr instead.
- A container that cannot be hashed (`__eq__` without `__hash__`) broke every
  project module imported after startup with a `TypeError`.
- A lock cycle found while closing a scope (the closing thread holds a lock
  that a creation in that scope waits for) left the scope closed but its
  objects open, and a second `close()` did nothing. Now nothing is closed
  while that lock is held: the scope stays open and whole (the creation
  finishes into it), the `CircularDependencyError` is raised, and closing it
  again without the lock closes every object - also those that can only be
  closed with `aclose()`.
- Two web applications in one process could serve one application's requests
  with the other application's services: patched classes, `injected()` and the
  field dependencies of registered services all use the active container, which
  is the one started last. Several applications in one process are not
  supported (that comes in 0.6.0, with a per-request container); 0.5.4 adds one
  simple check that catches the usual case: a request (FastAPI, Flask, Django)
  or an `injected()` call whose container is not the active one fails with
  `DependencyInjectionError` before anything is built. It is a best-effort
  catch, not a guarantee: it is made at the start of the request, so a
  container activated while the request runs, or an object built outside any
  request, is not caught. Applications used one after another, as in tests,
  keep working: each startup makes its own container the active one, and a
  stopped application's container stays active until then, so a patched class
  never falls back to an older container. Applications sharing one container
  (for example a mounted sub-application) are not affected. `injected()` fills
  a never-patched class through the same path as a patched `__init__`; with no
  active container at all (no application has started yet, as with only
  `TestClient`s used without `with`) nothing changes from 0.5.3: a field that is
  not found stays `None`, and its WARNING now says that startup has not run.
  Mixing the two is caught: after an application opened with `with` has
  stopped, its container stays active, so a request of another application used
  without `with` fails - open every application with `with`, or call
  `activate_container(container)` first.
- A registration changed after autowiring started (`replace()`, a later
  `register()` or `register_alias()`) did not reach new consumers until
  `refresh()` was called: `resolve()` already gave the new implementation while
  a patched class's field silently still got the old one (since 0.5.3). The
  active name map now rebuilds itself when the registrations change; an
  unchanged container never rebuilds it, and the lookup takes no lock. The old
  map is let go after the rebuild's lock: a `__del__` it triggers (a replaced
  factory, say) may look names up again without hanging. The same holds for
  `replace()`, `register_alias()`, a closing scope and `Container.set_default()`:
  what they let go is released after their lock, so no `__del__` runs under a
  library lock (one that resolved a singleton another thread was creating
  could hang both threads).
- A closing scope could hand out an object that was already disposed (a
  finalizer resolving a sibling finalized before it, or work a finalizer had
  started), and could hand its live objects to code that was not closing it
  (another thread or task holding an earlier copy of the context). Now the
  objects leave the lock-free path when the close starts, each one leaves the
  scope before its finalizer runs, and only the closing code (and what it
  passes its context to) still gets the ones not finalized yet; everyone else
  gets `ScopeNotActiveError`. The lock-free path of a live scope is unchanged.
- On the free-threaded 3.14 build a new thread inherits its starter's context,
  so a pool worker born inside one request kept that request's scope and served
  other requests from it - another request's session, silently. A thread's
  birth now leaves the library's own context values out (a `sys.monitoring`
  event local to `threading.Thread.start`; checked when installed). If it
  cannot be used, or fails later, a hard rule holds for the rest of the process
  (a scope is read only in the thread that entered it; one WARNING at import, the
  reason in every refusal's message afterwards; `_thread_birth.STATUS`).
  Behaviour change on that build only: a plain `threading.Thread` child of a
  request no longer sees the request's scope, as on 3.11 - pass the context
  explicitly to share it.
- The library's own source imported a second time under another name (by
  discovery) installed that thread hook a second time; it is installed once,
  and the second copy uses the hard rule for its own containers - also when
  both copies install at the same moment (never two hooks).
- `replace()` moves the aliases of the replaced type along (also a chain of
  aliases, and an alias of an earlier registration of that type). An alias
  used to keep the old registration, so a test that replaced a service with a
  fake still got the real one through the alias. Replacing the alias itself
  still leaves its target alone; `register()` still leaves an alias where it was.
- Speed: creating an object whose fields are injected was about 50% slower than
  in 0.5.3 during the 0.5.4 work. Almost all of it came from one step: every
  `resolve()` got per-call bookkeeping for deferred warnings, and an already
  created singleton paid for it too, although it takes no lock and has nothing
  to warn about. A ready singleton now returns at once (a closed container
  still raises). Measured with the same benchmark (one injected field,
  200 000 objects, 10 alternating rounds, CPython 3.11; median of the per-round
  differences): -0.9% against 0.5.3 (no slowdown left), -31.5% against the
  previous 0.5.4 step. The slowdown was missed because each step was only
  compared with the step before it (each stayed under the 5% limit, one step
  added 45%); the release is now also measured against the previous release.

### Behaviour changes (tests that changed with them)

Each changed test pinned a defect, not a contract:

- `test_n2_a_positional_only_dependency_is_injected` (was
  `..._is_a_validation_issue`): a positional-only dependency failed with
  `TypeError`, validation reported it -> it is injected, validation is clean.
  Code that worked keeps working.
- `test_n2_positional_only_with_provider_or_keyed_is_injected` (was
  `..._is_reported`): the same for `Provider[...]` and `Keyed` parameters.
- `test_uncertain_constructor_details_are_reported_like_runtime[opaque]`: a
  broken `__signature__` was "cannot be read" -> it is rebuilt and the real
  problem (a parameter without annotation or default) is reported. A new case
  `opaque_codeless` keeps the "cannot be read" path.
- `test_uncertain_constructor_details_are_reported_like_runtime[positional_only]`:
  was an issue -> passes (supported).
- `test_validation_reports_a_constructor_whose_signature_cannot_be_read` and
  `test_an_unreadable_signature_of_a_field_injection_class_is_a_warning_not_an_issue`:
  their constructor is now a callable without code (the only one whose
  signature still cannot be read); the plain function they used is rebuilt now.
- `test_y11_injected_names_a_field_it_cannot_fill` (was
  `..._with_an_unpatched_class_is_a_warning`): the fields of a never-patched
  class stayed `None` -> they are filled on the built object; the class is not
  patched; an unregistered field is still named in a WARNING.
- `test_y16_a_failed_metadata_pass_is_reported_as_a_hint_failure` (was
  `..._lost_keyed_metadata_is_a_warning`): a failed metadata pass made Keyed
  parameters unkeyed -> it is a type-hint failure like any other (one WARNING),
  never an unkeyed fallback.
- (no test changed) `replace()` with an alias: the alias kept the replaced
  registration -> it follows the replacement. This was listed as a known
  limitation; it is a defect, not a contract: `replace()` exists to put a fake
  in place of a service, and an alias silently bypassed it.
- (new tests) Two web applications running at the same time: one
  application's requests were served with the other's services, silently ->
  the request fails before anything is built (FastAPI, Flask, Django). This is
  the documented "one container per process" made explicit, not a new limit,
  and a best-effort catch, not a guarantee. The error names the fix for tests
  that use several applications one after another: open each with
  `with TestClient(app):` (its startup makes its container the active one) or
  call `activate_container(container)`:

  ```python
  def test_first():
      with TestClient(first_app) as client:    # its startup makes its container active
          client.get("/products")

  def test_second():
      with TestClient(second_app) as client:   # its startup makes its container active
          client.get("/orders")
  ```
- (new tests) `replace()` after autowiring started: the field of a patched
  class kept the replaced implementation until `refresh()` -> it gets the new
  one. `refresh()` is still accepted; it is no longer needed for this.
- `test_a_real_import_failure_found_later_is_a_warning_once`: the WARNING came
  at once -> the background writer writes it a moment later (still exactly
  once); the test waits for it.
- (new tests) A project class inheriting a `*_instance` field from a base
  OUTSIDE the project (set in the base's `__init__`, its class body, a mixin
  method or another method of the base): the field was never a candidate - the
  class was not patched, a registered field stayed `None` and an unregistered
  one said nothing, `validate=True` neither (since 0.4.0) -> such a base is read
  with the rule of the class's own members (its body, `__slots__`, annotations,
  and only the `self.` assignments in its methods); a registered field is
  filled, an unregistered one is a WARNING and a validation issue. The base
  itself is never patched. **What may change for working code:** an attribute
  a base outside the project keeps for itself under a `*_instance` name is now
  filled when a registration matches its name (only then). To keep it as it
  is, give it a value before the field is read, or rename the registration.
  Its plain methods are read for assignments on `self` (also a `self` a
  closure captures); in its static and class methods every `*_instance`
  assignment counts; an assignment whose object the scan cannot name counts -
  in doubt a field is reported (a WARNING, a validation issue), never lost. A
  base field or a base's annotation never makes the class a "field injection
  only" class: its constructor dependencies are still resolved (two first
  versions of this change left `__init__(self, dep: Dep = None)` with `None`;
  fixed before release). Before 0.5.4 a base's annotation alone already made
  the class skip its constructor dependencies; it no longer does.
- (no test changed) `injected()` was slower than in 0.5.3 - measured per
  commit: +95% for a registered singleton, +37% for a transient after the first
  0.5.4 step, and a cache decorator misplaced during the review (+130% for a
  transient, fixed before release). Now, against 0.5.3 (3.11, the dependency
  alone): singleton about +10%, transient about +9%, unregistered controller
  about +6%; see "Known limits" for what the rest costs and why it is kept.
  The library's per-request path (middleware, scope open and close, one
  dependency; no web framework), against 0.5.3: empty scope 12.6 -> 17.7 µs,
  registered singleton 14.0 -> 19.6 µs, scoped service 24.8 -> 49.7 µs. The
  scoped creation gate (concurrent creation correctness) is the largest part;
  its redesign is the first item of 0.5.5. A close that waits for a creation in
  flight no longer hands a ready object of that scope to another context
  (found in the final review; the object was about to be disposed).
- `test_a_singleton_not_created_yet_still_does_the_bookkeeping` and
  `test_other_lifetimes_still_do_the_bookkeeping`: every resolution counted a
  "resolution depth" to keep WARNINGs until creation locks were released ->
  the count is the library locks this thread holds (any of them: creation,
  library, registration, provider, scope, name map, thread birth, startup), so
  a WARNING raised under the library lock is kept too; a resolution that takes
  no lock (a transient) writes its WARNING at once instead of at the end of the
  resolution, and pays no bookkeeping (it was the largest part of injected()'s
  slowdown against 0.5.3).

Tests of 0.5.3 that the earlier 0.5.4 changes above also changed: the skip
message now ends with "it is checked when it loads"; a read-only property is
not a field (no getter warning, no validation issue); validation reports an
unreadable signature and an unresolvable hint; the bridges' exit hook is the
internal `_release_singletons_at_exit`.

### Coming in 0.6.0 (they change behaviour working code may rely on)

- Equally good candidates become a startup error, with an explicit "first
  registered" choice for code that wants today's behaviour.
- A singleton capturing a scoped service becomes an error (`Provider` is the
  way to use a scoped service from a singleton).
- Required and optional `*_instance` fields are told apart explicitly.
- Incomplete discovery (a file that cannot be imported, an unsupported loader)
  stops startup unless allowed.
- One construction plan shared by the container and validation, and a startup
  that imports user code outside the library lock.
- An explicit policy for properties whose getter fails.
- A `Keyed` parameter with a default whose type hints cannot be resolved fails
  instead of using the default.
- Several applications in one process: a per-request container (`ContextVar`)
  instead of one active container per process.

## 0.5.3 - 2026-10-21

### Fixed

- Startup logged a false WARNING `Import failed for <module>: cannot import name
  ... from partially initialized module ...` when a project file imports the
  application module while it is still starting (typically `main.py` doing
  `from app import app`). Discovery cannot load such a file before the
  application module finishes; 0.4 skipped it silently, 0.5.0-0.5.2 reported it
  as a failure. It is now recognised (the module it imports is still being
  imported) and logged at DEBUG only. Nothing is lost: the file's `*_instance`
  fields are filled when it is imported later. Real failures (missing package,
  syntax error, missing name in a finished module) are still WARNINGs.

## 0.5.2 - 2026-10-20

Several implementations of one contract, as in .NET DI and Spring, through the
same `*_instance` fields. Existing code behaves as before.

### Added

- Registering a contract again with another class keeps both:
  `register_scoped(IRepo, MemoryRepo)` + `register_scoped(IRepo, SqlRepo)`.
  - A field named after a class receives that class (`memory_repo_instance`).
  - The contract's own field receives the last registration, exactly as before
    and as `resolve()` (`repo_instance`).
  - A plural field receives all of them as a list, in registration order
    (`repo_instances`), like .NET `IEnumerable<T>` / Spring `List<T>`.
  - Each registration keeps its own lifetime; registering the same class again
    adds no second copy; `replace()` removes the earlier ones.
- `container.resolve_all(T)` (and `scope.resolve_all(T)`): every registration
  of `T` in registration order; an empty list if there is none.
- `validate=True` also checks every registration behind a plural field.

### Fixed

- Field names with upper case letters were silently ignored and stayed `None`
  (`sqlRepo_instance`, `SqlRepo_instance`), although the README said upper/lower
  case does not matter. All four spellings now work:
  `sql_repo_instance`, `sqlrepo_instance`, `sqlRepo_instance`, `SqlRepo_instance`.

## 0.5.1 - 2026-10-20

A simpler setup. Runtime field injection (`self.x_instance = None`) works
exactly as before.

### Added

- More contract prefixes match a field name: `I_Service`, and `ABSService`,
  `AbsService`, `absService` without an underscore (next letter upper case),
  besides the existing `IService`, `ABS_`/`Abs_`/`abs_` and the `Impl` suffix.
- Flask bridge: `py_autowired.flask.attach_container(app, container)`.
- Django bridge: `application = py_autowired.django.attach_container(application, container)`
  (WSGI and ASGI; no settings change).
  Both give every request its own scope (kept open while the response is sent),
  scan the project once on the first request, and dispose container-created
  singletons when the process exits.
- Four self-contained examples (`examples/console`, `fastapi`, `flask`,
  `django`), each with the same layers, a `register/` folder (the only place
  that imports the classes), a `bootstrap/` folder (container + registrations +
  bridge) and its own README in English and Turkish. Every registered service
  has a contract and the containers use `strict_interfaces=True`. The shared
  example folder is gone.

### Fixed

- No `*_instance` field is missed because of where or when its module loads:
  a project module imported after startup - a lazy import, a `_private.py`
  file, a class in a package `__init__.py`, or a file in a folder that
  discovery skips (`dist`, `env`, `tests`, ...) - is patched the moment it is
  imported. Modules outside the project are never touched.
- Registering only an interface (`register_scoped(IRepo)`) no longer fails when
  the implementation lives in another, not yet imported layer: the
  implementation is looked up again after the startup scan or on first resolve.
  A missing implementation is reported when resolving (and by `validate=True`).
- `attach_container` (FastAPI) needs no `root_dir`: the project root is
  searched from the file that calls it, so `uvicorn main:app` works too.
  Previously the root was guessed from `__main__`, which is the server under
  `uvicorn main:app`, and the fields stayed `None`.
- `attach_container` (FastAPI) now disposes container-created singletons when
  the application stops; a later startup creates fresh ones. Turn it off with
  `close_on_shutdown=False`. Objects given to `register_instance` are never
  disposed.
- Discovery skipped every module of a project that itself lives under a folder
  named `examples`, `tests`, `build`, ... Excluded names now apply only to
  folders inside the project.

### Changed

- The distribution name is written `py_autowired` (PyPI treats it as the same
  project as `py-autowired`; `pip install` works with either spelling).
- README and the Turkish guide rewritten: quick start, the three rules,
  `container.register_*`, frameworks, then every optional feature in its own
  section. `USAGE_GUIDE_EN.md` now points to the README.

## 0.5.0 - 2026-10-20

### Fixed

- Python 3.10 no longer differs from 3.11+: its `get_type_hints` turns
  `dep: T = None` into `Optional[T]`, so the dependency was silently left `None`
  on 3.10 only. That implicit Optional is now ignored; an explicitly written
  `Optional[T]`/`T | None` keeps its previous behavior.

- Concurrency (no new lock on any hot path; ready singletons and cache hits stay
  lock-free): a selection computed from a pre-`refresh()` map is never cached;
  closing a scope waits for an in-flight creation and disposes it (finalizers run
  outside the lock, before the scope cache is cleared, as before); a cross-thread
  cycle in first singleton creation raises `CircularDependencyError` instead of
  deadlocking (wait-for detection only on a contended first creation).

- A synchronous factory that returns a bare coroutine now raises
  `AsyncFactoryError` (the coroutine is closed) instead of injecting/caching an
  unexecuted coroutine. Other awaitables (pools, futures) are returned unchanged.
- Disposal still raises the first exception (same object and type); additional
  errors are no longer lost: they are attached as notes (`add_note` on 3.11+,
  `__notes__` on 3.10) and logged at ERROR.
- Discovery no longer counts a same-name module imported from another file as
  successful discovery of the expected file; it logs WARNING with both paths
  and preserves the existing import.
- `AutoInjectionResolutionError` remains a `RuntimeError` and also inherits
  `DependencyInjectionError`.
- Publication requires the same commit's complete reusable `tests.yml` workflow:
  Python 3.10, 3.14 and 3.14t.

### Added

- Mutation testing report (cosmic-ray, injection engine and container):
  99.3% of behavior-changing mutants killed; equivalent mutants and the 9 open
  ones are listed with their reasons in `docs/MUTATION_REPORT.md`.
- Opt-in open generics (.NET open generic registration):
  `register_open_generic(Repository, SqlRepository, scope)` serves
  `Repository[User]`, `Repository[Order]`, ... on demand, with one lifetime slot
  per closed type; TypeVar constructor parameters are substituted. Closed
  generics resolve through `resolve()`, constructor injection and validation, and
  are not part of name-based field injection. Registered types keep the same
  lookup path.

- Opt-in keyed services (.NET keyed services / Spring `@Qualifier`):
  `register_keyed(Type, key, Impl, scope)`, `resolve_keyed(Type, key)` and
  constructor parameters annotated `Annotated[Type, Keyed(key)]`. Keyed
  registrations are separate from name-based field injection (unchanged); each
  key has its own singleton/scoped instance and cycle detection.

- Opt-in `Container.set_primary(Type)` (like Spring's `@Primary`): when several
  registrations match an `*_instance` field, primary ones are preferred. Without
  a primary mark, selection is exactly as before. Validation applies the same rule.

- Opt-in deferred resolution: `Provider[T]` resolves on every call (respecting the
  registered lifetime and the scope active at call time), `Lazy[T]` resolves once
  on first `.value` access. Constructor parameters annotated with them are filled
  automatically; both can also be built directly (`Provider(container, T)`).
  A singleton holding `Provider[ScopedService]` does not capture the scoped
  service, and opt-in validation accepts it. Field injection is unchanged.

- Opt-in application shutdown: `Container.close()` / `await Container.aclose()`
  dispose container-created singletons in reverse creation order, once per object.
  Following .NET, instances supplied through `register_instance` are owned by the
  caller and are not disposed. After closing, `resolve()` raises
  `ContainerClosedError`. Call it after application work has stopped. No new lock
  is taken on the resolution path.

- Opt-in validation reports required untyped constructor parameters, singleton
  capture of scoped dependencies through fields, constructors or declared factory
  metadata, and inherited unannotated fields excluded from post-init injection.
  Runtime injection and lifetime rules are unchanged.

### Changed (diagnostics only)

- WARNING logs are emitted by default for field-read failures (with traceback) and
  discovery failures or unexpected module origins. These logs only add diagnostics;
  field skipping and discovery behavior are unchanged. Tied candidates are not logged
  at runtime; opt-in validation reports them. Logging handlers must not resolve
  services from the same container.

### Known limitations

- After `replace()`, an alias still points to the previous registration.
- Inherited unannotated fields gain no new injection.

(Status in 0.5.4: neither holds any more - `replace()` moves aliases along, and
inherited unannotated fields are injected.)

### Earlier changes in this release

- Added opt-in `validate_autowiring()` and
  `auto_inject(..., validate=True)` / `attach_container(..., validate=True)`.
  `AutowiringValidationError.issues` aggregates unmatched fields, tied candidates,
  and statically unresolvable registrations. The check does not construct
  objects/services; it may evaluate type annotations that execute user code.
- Replaced diagnostic library prints with English records on the `py_autowired` logger.
  Debug mode adds a console StreamHandler; otherwise the library adds only a
  NullHandler. Discovery import failures always emit logging WARNING records,
  never Python warnings. `print_cache_stats()` writes to stdout (unchanged).
- Made typed `*_instance: Service | None = None` fields the primary documentation
  example, and added startup validation, pytest replacement recipes, measured
  dataclass/slots/Pydantic compatibility, optional attrs tests, and a local
  `timeit` benchmark with recorded machine-dependent results.
- Added validation, logging, compatibility, replacement, and runtime regressions;
  matching results/order, explicit assignments, and unmatched `None` behavior
  are preserved. Existing tests remain intact.
- Added the optional `py_autowired.fastapi` bridge: `attach_container()` installs
  startup autowiring and one async scope per HTTP request; `injected()` supplies
  automatically populated objects to FastAPI `Depends`.
- Preserved application lifespans and request scopes through streaming,
  background work, errors, and synchronous/asynchronous resource disposal.
- Documented lifetime guidance: a singleton should not retain a scoped service
  in a field; use a scoped owner. Opt-in validation now checks static captures,
  with no runtime lifetime change.
- Shielded FastAPI request-scope cleanup from active AnyIO cancellation and
  wrapped the entire Starlette stack so user middleware and all exception
  handlers, including Exception/500 handlers, retain the same request scope.
- Made ordinary runtime controller construction the primary FastAPI example,
  with FastAPI-style Depends injection as an optional alternative.
- Added a Python 3.14t job that asserts the GIL is disabled, and a manual-only
  mutation workflow targeting autowired.py and container.py with summary artifacts.
- Added the `fastapi` extra, typing classifier, runtime injection keywords,
  FastAPI example, concurrency/disposal tests, and automatic injection regressions.
- Corrected package layout and factory signature documentation, and documented
  the process-wide active container limit and trusted module discovery.
- Automatic field matching, injection order, explicit assignments, unmatched
  fields, and framework independence are preserved; container fixes appear above.

## 0.4.0 - 2026-08-30

- Added `__init__.py`, so `from py_autowired import Container` now works.
  Submodule imports such as `from py_autowired.container import Container`
  are unchanged and return the same objects.
- Added the PEP 561 `py.typed` marker, so type checkers now read the
  package's annotations.
- Added `Container.set_implementation_selector()`, which picks an
  abstraction's implementation at registration time instead of raising
  `AmbiguousImplementationError`.
- Classes carrying the `__di_attrs__` field-injection marker are now built
  without constructor resolution only when their `__init__` needs no
  arguments. A class that combines field injection with required
  constructor dependencies still receives them.
- Cached the constructor signature inspection that runs on the instance
  creation path.
- No new constructor settings and no changed defaults; 0.3.x code runs
  unchanged on 0.4.0.


## 0.3.0 - 2026-08-30

- Added `Container.register_alias()` with shared lifetime and instance semantics.
- Made name-based injection aware of public alias binding names.
- Added the optional `auto_inject(..., exclude_dirs=...)` extension point.
- Preserved the existing discovery exclusions when `exclude_dirs` is omitted.
- Added the opt-in `__di_compat_aliases__` class attribute, which mirrors
  assignments on legacy field names onto their canonical `*_instance` marker.
- All changes are additive; 0.2.x code runs unchanged on 0.3.0.


## 0.2.1 - 2026-08-28

- Added canonical GitHub project metadata and the full author name.
- Added verified GitHub Actions CI for Python 3.10 and 3.14.
- Added the optional httpx2 dependency required by the current Starlette TestClient.
- Updated GitHub Actions to their Node 24-compatible releases.

## 0.2.0

- Restored the original runtime injection rule: self.x_instance = None.
- Removed provider-based and application-side resolve usage.
- Reduced the runtime package to autowired.py and container.py.
- Added layered Console, FastAPI, Flask and Django examples.
