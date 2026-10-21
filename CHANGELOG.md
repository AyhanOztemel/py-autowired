# Changelog

## 0.5.4 - 2026-10-21

Nothing hides any more: no problem stays behind a `try/except`, a DEBUG-only
line or a quiet default. Nothing is removed; what gets injected does not change.

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
    value), a keyed singleton capturing a scoped field, a positional-only
    constructor parameter (the container passes arguments by name), and every
    project module discovery could not import or a late import could not patch;
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
- Validation no longer passes what the container would fail on: an unreadable
  constructor signature and a type hint that cannot be resolved (without a
  default) are now issues. A non-callable `__init__` fails as Python makes it
  fail (the patched constructor used to swallow it).

### Fixed

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
