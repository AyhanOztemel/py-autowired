"""
Automatic field injection with cached name matching and nested module scanning.
"""
import re
import inspect
import types
import sys
import importlib
import importlib.abc
import importlib.machinery
import os
import threading
import time
import dis
import logging
import weakref
from functools import wraps
from pathlib import Path
from collections import defaultdict
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Any, Tuple, TypeVar

from .container import (
    _BACKGROUND_LOG, DependencyInjectionError, Lazy, LifetimeScope, _WARNED, _constructor_hints,
    _HeldLogger, _LibraryLock, _deferred_wrapper, _field_injection_only, _hold, _init_signature, _keyed_marker,
    _unhold, _parameter_hint, _signature_of, _slot_name,
    _unsupported_keyed,
    _unwrap_annotation, _warn_once, _warn_once_background,
)

# ============================================================================
# CONFIGURATION
# ============================================================================
DEBUG_MODE = os.getenv("PY_AUTOWIRED_DEBUG", os.getenv("DI_DEBUG", "")).lower() in ("1", "true", "yes")
logger = _HeldLogger(logging.getLogger("py_autowired"))  # kept while a library lock is held
# Only debug mode installs console output. Applications otherwise own logging.
if not any(getattr(handler, "_py_autowired_handler", False) for handler in logger.handlers):
    handler = logging.StreamHandler() if DEBUG_MODE else logging.NullHandler()
    handler._py_autowired_handler = True
    logger.addHandler(handler)
if DEBUG_MODE:
    logger.setLevel(logging.DEBUG)
# 🆕 v2: Eski davranış (TÜM sınıfları patch'le) için kaçış kapısı.
#        Varsayılan: sadece '*_instance' adayı olan sınıflar patch'lenir.
PATCH_ALL_MODE = os.getenv("PY_AUTOWIRED_PATCH_ALL", os.getenv("DI_PATCH_ALL", "")).lower() in ("1", "true", "yes")
# A field ends in _instance (one object) or _instances (every registration of a
# contract). Upper and lower case are both accepted: product_repository_instance,
# productRepository_instance and ProductRepository_instance name the same thing.
_DEF_ATTR_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*_instances?$')
_ALL = "*"  # name-map key prefix of plural (*_instances) fields

# 🆕 v2: Keşif sırasında atlanacak dizinler (.venv tuzağı düzeltmesi)
_EXCLUDED_DIRS = {
    ".venv", "venv", "env", ".env", "site-packages",
    "__pycache__", ".git", "node_modules", "build", "dist",
    ".tox", ".mypy_cache", ".pytest_cache", "egg-info",
    "tests", "examples",
}

# Global caches
_NAME_MAP_CACHE: Optional['_CachedNameMap'] = None
# 🆕 v2: nogil/free-threaded güvenliği için global kilit
_GLOBAL_WAIT_WARNING = 10.0  # seconds a thread may wait for the library lock before a WARNING


class _GlobalLock:
    """The library's re-entrant lock. A blocking acquire that has to wait polls;
    after _GLOBAL_WAIT_WARNING seconds it reports, once, which thread holds the
    lock (the holder may be waiting for this one). Uncontended: one acquire."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: Optional[int] = None
        self._depth = 0
        self._kept = threading.local()  # what let_go() keeps, per thread, until the lock is free

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking or timeout >= 0:
            got = self._lock.acquire(blocking, timeout)
        else:
            got = self._lock.acquire(blocking=False) or self._wait()
        if got:
            self._owner = threading.get_ident()
            self._depth += 1
            _hold()
        return got

    def _wait(self) -> bool:
        started, warned = time.monotonic(), False
        while not self._lock.acquire(timeout=0.05):
            if not warned and time.monotonic() - started >= _GLOBAL_WAIT_WARNING:
                warned = True
                _BACKGROUND_LOG.put(
                    "thread %s has waited %.1fs for the py_autowired lock held by thread %s; "
                    "if that thread waits for this one (for example thread.join()), this is a deadlock",
                    (threading.get_ident(), time.monotonic() - started, self._owner))
        return True

    def release(self) -> None:
        self._depth -= 1
        outermost = not self._depth
        if outermost:
            self._owner = None
        self._lock.release()
        if outermost:
            kept = self._kept.__dict__.pop("objects", None)
            del kept  # after the lock: a __del__ that raises is reported by Python, the rest still go
        _unhold()  # the last library lock released: the WARNINGs kept meanwhile are written

    def let_go(self, obj: Any) -> None:
        """Release ``obj`` only once this thread no longer holds the lock: its last reference may
        run a user's __del__, which must not run under the library lock. Not held: nothing kept."""
        if self._owner == threading.get_ident():
            self._kept.__dict__.setdefault("objects", []).append(obj)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


_GLOBAL_LOCK = _GlobalLock()
_PATCHED_CLASSES = {}


_FORGOTTEN: list = []  # (store, key, value) of containers collected while the lock was busy


def _forget(store, key, value):
    """Finalizer of a collected container. The garbage collector runs it in any
    thread - maybe while another thread holds the lock and waits for this one -
    so it never waits for the lock; the next lock holder finishes the job."""
    if _GLOBAL_LOCK.acquire(blocking=False):
        try:
            _drop(store, key, value)
        finally:
            _GLOBAL_LOCK.release()
    else:
        _FORGOTTEN.append((store, key, value))


def _drop(store, key, value):
    # id() values are reused: drop the entry only if it still belongs to the
    # collected container, never the entry of a newer container with that id.
    if store.get(key) is value:
        del store[key]


def _forget_patched_classes(key, classes):
    _forget(_PATCHED_CLASSES, key, classes)


def _forget_untracked(key, entries):
    _forget(_UNTRACKED, key, entries)


_UNTRACKED: Dict[int, List[str]] = {}  # id(container) -> issues validation must report
_PINNED: Dict[int, Any] = {}  # id -> container that cannot be weakly referenced


def _record_issue(container, text: str) -> None:
    """An issue validate_autowiring() reports for ``container``; dropped with it."""
    with _GLOBAL_LOCK:
        _forget_collected()
        key = id(container)
        entries = _UNTRACKED.get(key)
        if entries is None:
            entries = _UNTRACKED[key] = []
            try:
                weakref.finalize(container, _forget_untracked, key, entries)
            except TypeError:
                # No finalizer possible: keep the container alive, so its id can
                # never be given to another container that would inherit these.
                _PINNED[key] = container
        if text not in entries:
            entries.append(text)


def _track(container, cls) -> None:
    """Remember ``cls`` for validation. A failure is never silent: it is a
    WARNING now and an issue in validate_autowiring()."""
    try:
        _remember_patched_class(container, cls)
    except Exception as error:
        label = f"{getattr(cls, '__module__', '?')}.{getattr(cls, '__qualname__', cls)}"
        error = repr(error)  # text only: a kept log record must not keep the container alive
        logger.warning("%s could not be tracked for validation; validate_autowiring() reports it: %s",
                       label, error)
        _record_issue(container, f"{label}: could not be tracked for validation ({error})")


def _forget_collected() -> None:
    """Finish the finalizers that found the lock busy. Caller holds the lock."""
    while _FORGOTTEN:
        try:
            _drop(*_FORGOTTEN.pop())
        except IndexError:
            break


def _remember_patched_class(container, cls):
    # Identity keys avoid hashing containers or classes with custom metaclasses.
    with _GLOBAL_LOCK:
        _forget_collected()
        key = id(container)
        if key not in _PATCHED_CLASSES:
            _PATCHED_CLASSES[key] = weakref.WeakValueDictionary()
            try:
                weakref.finalize(container, _forget_patched_classes, key, _PATCHED_CLASSES[key])
            except TypeError:
                # A non-weakrefable duck-typed container remains supported; it is
                # kept alive so that its id is never reused by another container.
                _PINNED[key] = container
        _PATCHED_CLASSES[key][id(cls)] = cls

class AutowiringValidationError(DependencyInjectionError):
    """All statically detectable field problems from a startup check.

    ``issues`` is an immutable tuple of English diagnostic strings.
    """

    def __init__(self, issues, skipped=()):
        self.issues = tuple(issues)
        lines = [f"- {issue}" for issue in self.issues]
        if skipped:
            lines.append("Not checked yet (skipped during discovery; checked when they load): "
                         + ", ".join(skipped))
        super().__init__("Autowiring validation failed:\n" + "\n".join(lines))


class AutoInjectionResolutionError(DependencyInjectionError, RuntimeError):
    """A registered dependency existed but could not be constructed."""



# ============================================================================
# OPTIMIZED CORE FUNCTIONS
# ============================================================================

@lru_cache(maxsize=512)
def _norm(s: str) -> str:
    """✅ Cached normalization - pure function"""
    return s.replace("_", "").lower()


@lru_cache(maxsize=1024)
def _field_key(name: str):
    """Name-map key of a field: 'sqlRepo_instance' -> 'sqlrepo',
    'repo_instances' -> ('*', 'repo') (every registration of the contract)."""
    if name.endswith("_instances"):
        return (_ALL, _norm(name[:-len("_instances")]))
    return _norm(name[:-len("_instance")])


def _generic_key(name: str) -> Optional[str]:
    """The name without its contract prefix or Impl suffix, if it has one."""
    # ABS_Service / Abs_Service / abs_Service -> service
    if name.startswith('ABS_') and len(name) > 4:
        return _norm(name[4:])
    elif name.startswith('Abs_') and len(name) > 4:
        return _norm(name[4:])
    elif name.startswith('abs_') and len(name) > 4:
        return _norm(name[4:])
    # I_Service -> service
    elif name.startswith('I_') and len(name) > 2:
        return _norm(name[2:])
    # ABSService / AbsService / absService -> service (no underscore; the next
    # letter must be upper case, so "Absolute" stays a word)
    elif name[:3] in ('ABS', 'Abs', 'abs') and len(name) > 3 and name[3].isupper():
        return _norm(name[3:])
    # IService -> service
    elif name.startswith('I') and len(name) > 1 and name[1].isupper():
        return _norm(name[1:])
    # ServiceImpl -> service
    elif name.endswith('Impl'):
        return _norm(name[:-4])
    return None


def _contract_keys(service_type) -> set:
    """Keys under which a contract itself is found (full name and generic name)."""
    keys = {_norm(service_type.__name__), _generic_key(service_type.__name__)}
    keys.discard(None)
    return keys


@lru_cache(maxsize=1024)
def _calculate_module_similarity(mod1: str, mod2: str) -> int:
    """✅ Cached similarity calculation - pure function"""
    parts1 = mod1.split('.')
    parts2 = mod2.split('.')

    common_depth = 0
    for i in range(min(len(parts1), len(parts2))):
        if parts1[i] == parts2[i]:
            common_depth += 1
        else:
            break

    return common_depth


def _get_module_depth_info(module_name: str) -> dict:
    """
    Module depth info - returns fresh dict each time.
    No caching to avoid mutable data issues.
    """
    parts = module_name.split('.')
    return {
        'full_name': module_name,
        'parts': parts,
        'depth': len(parts),
    }


def _is_rel_to(path: Path, base: Path) -> bool:
    """Fast relative path check"""
    try:
        path.relative_to(base)
        return True
    except Exception:
        return False


# ============================================================================
# CACHED NAME MAP - Internal optimization, transparent to users
# ============================================================================

class _CachedNameMap:
    """
    Internal cache for name map and resolutions.
    Provides massive speedup while maintaining backward compatibility.
    """
    __slots__ = ('_raw_map', '_resolution_cache', '_container', '_lock', '_generation', '_seen')

    def __init__(self, container):
        self._container = container
        self._raw_map = None
        self._resolution_cache: Dict[Tuple[str, str], Any] = {}
        # 🆕 v2: nogil güvenliği — lazy init yarışını kapatan kilit
        self._lock = _LibraryLock()
        # Bumped by every rebuild (refresh() and a registration change both count).
        self._generation = 0
        # The container's change count the map was built from (None: not built yet).
        self._seen = None

    def _stale(self) -> bool:
        return self._raw_map is None or getattr(self._container, "_changes", 0) != self._seen

    def get_raw_map(self) -> dict:
        """The name map: built once, and again when the registrations have changed."""
        # Double-checked locking: no lock on the hot path
        if self._stale():
            built = retired = None
            with self._lock:
                if self._stale():
                    built, retired = self._rebuild()
            _GLOBAL_LOCK.let_go(retired)  # under the library lock: kept until that is free too
            del retired  # after the lock: dropping the old map may run a user's __del__
            if built is not None:
                _log_name_map(built)  # after the lock: a log handler may inject fields itself
        return self._raw_map

    def _rebuild(self):
        """Build the map and drop the cached choices (the caller holds self._lock; no log
        and no user code here). Returns the new map and the old map and choices: the caller
        drops them after the lock (their last reference may run a user's __del__)."""
        retired = (self._raw_map, self._resolution_cache)
        seen = getattr(self._container, "_changes", 0)  # read first: a change meanwhile rebuilds again
        self._generation += 1
        self._raw_map = _build_name_map_from_container(self._container, log=False)
        # A NEW dict, after the new map: a lookup that took the old dict (with the old map) can only
        # store its old choice there, where no lookup reads any more.
        self._resolution_cache = {}
        self._seen = seen
        return self._raw_map, retired

    def refresh(self):
        """🆕 v2: Geç kayıtlar için haritayı ve cache'i yeniden kur."""
        with self._lock:
            built, retired = self._rebuild()
        _GLOBAL_LOCK.let_go(retired)  # under the library lock: kept until that is free too
        del retired  # after the lock: dropping the old map may run a user's __del__
        _log_name_map(built)

    def get_cached_registration(self, normalized_name: str, context_module: str) -> Optional[Any]:
        """Get best registration with aggressive caching"""
        try:  # read directly: getattr() with a default costs a call on every lookup
            changed = self._container._changes != self._seen
        except AttributeError:  # a container that counts no changes
            changed = 0 != self._seen
        if changed:
            self.get_raw_map()  # the registrations changed: never answer from the old choices
        cache_key = (normalized_name, context_module)

        # O(1) cache lookup (lock-free hot path, as in 0.4.0), read once: a rebuild may
        # clear the choices at any moment
        cached = self._resolution_cache.get(cache_key, _UNSET)
        if cached is not _UNSET:
            return cached

        # Cache miss - calculate and store (still lock-free). The choices dict is taken BEFORE the
        # map: a rebuild meanwhile gives the cache a new dict, so a choice made from the old map
        # goes into the old dict only - it never reaches a later lookup.
        choices = self._resolution_cache
        raw_map = self.get_raw_map()
        regs = raw_map.get(normalized_name, [])
        best = _pick_best_registration(regs, context_module) if regs else None
        # 🆕 v2: setdefault — eşzamanlı yazımda tek deterministik değer
        return choices.setdefault(cache_key, best)

def _build_name_map_from_container(container, log=True):
    """
    ORIGINAL FUNCTION - unchanged logic
    Build name map from container registrations. ``log=False``: the caller logs it
    later, outside its lock.
    """
    name_map = defaultdict(list)

    # Snapshot copy; no lock here (never nest container and cache locks).
    for binding_type, reg in list(container.registrations.items()):
        # Aliases share a registration with their target. Index the public
        # binding name so name-based injection can resolve either contract.
        st = binding_type
        it = reg.implementation_type or st

        # Interface name
        key1 = _norm(st.__name__)
        name_map[key1].append(reg)

        # Implementation name
        if it != st:
            key2 = _norm(it.__name__)
            name_map[key2].append(reg)

        # Generic names (prefix / Impl suffix removed)
        for typ in {st, it}:
            generic_key = _generic_key(typ.__name__)
            if generic_key is not None:
                name_map[generic_key].append(reg)

        # Plural fields (repo_instances) find the contract by its own names.
        for key in _contract_keys(st):
            name_map[(_ALL, key)].append(reg)

    # Earlier registrations of a contract (register_scoped(IRepo, MemoryRepo)
    # followed by register_scoped(IRepo, SqlRepo)) are found by their class
    # name only; the contract's own names keep answering with the current one.
    for st, earlier in list(getattr(container, "_earlier", {}).items()):
        taken = _contract_keys(st)
        for reg in list(earlier):
            it = reg.implementation_type
            if it is None or it is st:
                continue
            for key in {_norm(it.__name__), _generic_key(it.__name__)} - taken - {None}:
                name_map[key].append(reg)

    if log:
        _log_name_map(name_map)
    return name_map


def _log_name_map(name_map) -> None:
    if DEBUG_MODE:
        logger.debug("Name map built:")
        for key, regs in name_map.items():
            types = [r.service_type.__name__ for r in regs]
            logger.debug(f"  {key!r} -> {types}")


def _registration_score(reg, ctx_module: str):
    """Shared runtime/validation rank; equal scores keep registration order."""
    if not ctx_module:
        return (0, 0)
    impl = reg.implementation_type or reg.service_type
    module = getattr(impl, "__module__", "")
    if not module:
        return (-1, 0)
    depth = _get_module_depth_info(module)['depth']
    score = 1000 if module == ctx_module else _calculate_module_similarity(ctx_module, module) * 10 + depth
    return (score, depth)


def _pick_best_registration(regs, ctx_module: str):
    """
    ORIGINAL FUNCTION - unchanged logic
    Pick best registration based on module proximity.
    """
    if not regs:
        return None

    # Opt-in @Primary: only narrows the candidates when a primary mark exists.
    primaries = [reg for reg in regs if getattr(reg, "primary", False)]
    if primaries:
        regs = primaries

    if not ctx_module:
        return regs[0]

    scored_regs = []

    for reg in regs:
        it = reg.implementation_type or reg.service_type
        reg_module = getattr(it, "__module__", "")

        score, depth = _registration_score(reg, ctx_module)
        scored_regs.append((reg, score, depth))

        if not reg_module:
            continue

        if DEBUG_MODE:
            logger.debug(f"{it.__name__} ({reg_module}): score={score}, common_depth={_calculate_module_similarity(ctx_module, reg_module)}")

    # Sort by score
    scored_regs.sort(key=lambda x: (x[1], x[2]), reverse=True)

    selected = scored_regs[0][0]

    if DEBUG_MODE:
        selected_type = selected.implementation_type or selected.service_type
        logger.debug(f"Selected: {selected_type.__name__} (module: {getattr(selected_type, '__module__', 'unknown')})")

    return selected


def _resolve_via_container(container, reg, every=False):
    """Resolve the selected registration; ``every`` returns the whole list."""
    if every:
        return container.resolve_all(reg.service_type)
    if getattr(container, "registrations", {}).get(reg.service_type, reg) is reg:
        return container.resolve(reg.service_type)
    # An earlier registration of the contract, selected by its class name.
    return container._resolve_registration(reg)

def _lookup_registration(container, name_map, normalized_name: str, context_module: str):
    """Return the registration and the container that owns its active cache.

    Patched classes live longer than a bootstrap container in tests and in
    controlled runtime rebuilds. Looking up from the new cache but resolving
    through the old closure would mix two object graphs.
    """
    if _AWAITING:  # one dict check; empty once skipped files have loaded
        _load_skipped()
    cache = _NAME_MAP_CACHE
    if cache is not None:
        return (
            cache._container,
            cache.get_cached_registration(normalized_name, context_module),
        )
    return (
        container,
        _pick_best_registration(name_map.get(normalized_name, []), context_module),
    )




# ============================================================================
# CLASS PATCHING
# ============================================================================

def _find_di_candidates(cls) -> tuple:
    return _find_di_candidate_parts(cls)[0]


def _find_di_candidate_parts(cls) -> tuple:
    """(every candidate, the class's OWN candidates - steps 1-3, without the fields only a base
    outside the project gives it).

    🆕 v2: Sınıfın '*_instance' DI adaylarını PATCH ANINDA bir kez tespit et.
    Kaynaklar:
      1. Sınıf gövdesindeki attribute'lar   (order_service_instance = None)
      2. Annotation'lar                     (svc_instance: ABS_Service = None)
      3. Metod gövdelerindeki attribute
         isimleri (co_names)                (self.repo_instance = None)
    Böylece çalışma anında dir() taraması tamamen kalkar.
    """
    candidates = set()

    # 1) Sınıf gövdesi attribute'ları
    for name in cls.__dict__:
        if _DEF_ATTR_RE.match(name):
            candidates.add(name)

    # 2) Annotation'lar (kalıtım dahil — __init__ super zinciriyle set edebilir). A base's
    #    annotation is a candidate, not one of the class's OWN (0.5.4 round 20): it never makes
    #    the class a "field injection only" class.
    inherited = set()
    for klass in cls.__mro__:
        for name in getattr(klass, "__annotations__", {}):
            if _DEF_ATTR_RE.match(name):
                (candidates if klass is cls else inherited).add(name)

    # 3) Kendi metodlarının co_names'i (self.x_instance = ... atamalarını yakalar)
    for member in cls.__dict__.values():
        fn = getattr(member, "__func__", member)  # staticmethod/classmethod aç
        code = getattr(fn, "__code__", None)
        if code is None:
            continue
        for instruction in dis.get_instructions(fn):
            if instruction.opname != "STORE_ATTR":
                continue
            if _DEF_ATTR_RE.match(instruction.argval):
                candidates.add(instruction.argval)

    # 4) Members inherited from bases OUTSIDE the project: such a base is never patched, so its
    #    field would otherwise be no candidate anywhere and stay None without a word (0.5.4).
    own = set(candidates)
    candidates.update(inherited)
    for base in _TYPE_MRO(cls)[1:]:
        candidates.update(_outside_base_fields(base))

    # A method, property or other descriptor with a field-like name (for example
    # a ``has_instance`` property) is not a field: injection must not touch it.
    every = tuple(sorted(name for name in candidates if not _is_accessor(cls, name)))
    return every, tuple(name for name in every if name in own)


_BASE_FIELDS: Dict[int, tuple] = {}  # id(base) -> (weak reference, its field names)


def _outside_base_fields(base) -> frozenset:
    """The *_instance names a class outside the project defines, read with the rule of a class's
    own members: its body (fields, __slots__) and the ``self.`` assignments in its methods. Empty
    for a project class (it is patched itself). Read once per class, from plain copies only: no
    code of the base runs."""
    cached = _BASE_FIELDS.get(id(base))
    if cached is not None and cached[0]() is base:
        return cached[1]
    names = frozenset() if _in_project(base) else _read_outside_base(base)
    try:
        _BASE_FIELDS[id(base)] = (weakref.ref(base, _forget_base(id(base))), names)
    except TypeError:
        pass  # it cannot be weakly referenced: read again next time
    return names


def _forget_base(key: int):
    def forget(ref) -> None:  # the class is gone: drop its entry
        entry = _BASE_FIELDS.get(key)
        if entry is not None and entry[0] is ref:
            del _BASE_FIELDS[key]
    return forget


def _read_outside_base(base) -> frozenset:
    snapshot = _plain_snapshot(_TYPE_DICT(base))
    if snapshot is None:
        # The library lock may be held here: a log handler never runs under it.
        _warn_once_background(("base-unreadable", _TYPE_MODULE(base), _TYPE_QUALNAME(base)),
                              "%s.%s: its __dict__ has a key that is not a plain str - its "
                              "*_instance fields are not looked for, so a project class "
                              "inheriting one is not filled from it",
                              _TYPE_MODULE(base), _TYPE_QUALNAME(base))
        return frozenset()
    try:
        names = set()
        for key, member in snapshot.items():
            if _DEF_ATTR_RE.match(key):
                names.add(key)
            if type(member) is types.FunctionType:
                names.update(_self_stores(member))
            elif type(member) is staticmethod or type(member) is classmethod:
                # Its first parameter is not known to be the object (it may be given self): every
                # assignment counts - a field reported in doubt, never one lost (round 20).
                function = member.__func__
                if type(function) is types.FunctionType:
                    names.update(_every_store(function))
        return frozenset(names)
    finally:
        # The copy may hold the last reference of a value removed meanwhile: it is dropped after
        # the library lock is released (its __del__ is user code).
        _GLOBAL_LOCK.let_go(snapshot)


def _self_stores(function) -> set:
    """The *_instance names ``function`` assigns on its first parameter (``self.x_instance = ...``);
    an assignment on any other object is not one of the class's fields. On a Python whose bytecode
    the check below does not recognise, every assignment counts (never fewer fields)."""
    if not _self_stores_recognised():
        return _every_store(function)
    return _owner_stores(function)


def _every_store(function) -> set:
    """Every *_instance name ``function`` assigns, whatever the object."""
    return {instruction.argval for instruction in dis.get_instructions(function)
            if instruction.opname == "STORE_ATTR" and _DEF_ATTR_RE.match(instruction.argval)}


_STORE_PROBE: list = []  # [True/False] once known: does _owner_stores read this Python's bytecode?


def _self_stores_recognised() -> bool:
    """Checked once per process on known shapes: a plain store, a store of a parameter (a paired
    load on 3.13+), a store on another object, a store past the 256th name (EXTENDED_ARG), a store
    on a ``self`` a closure captures (LOAD_DEREF). If this
    Python's bytecode differs, one WARNING says so and every assignment counts from then on."""
    if not _STORE_PROBE:
        namespace: Dict[str, Any] = {}
        exec("def probe(self, other, value):\n"
             "    self.a_instance = None\n    self.b_instance = value\n    other.c_instance = None\n"
             + "".join(f"    self.n{i} = None\n" for i in range(300))
             + "    self.d_instance = None\n", namespace)
        exec("def captured(self):\n    self.e_instance = None\n    return lambda: self\n", namespace)
        recognised = (_owner_stores(namespace["probe"]) == {"a_instance", "b_instance", "d_instance"}
                      and _owner_stores(namespace["captured"]) == {"e_instance"})
        if not recognised:
            # The library lock may be held here: a log handler never runs under it.
            _warn_once_background(("bytecode-unrecognised",),
                                  "The bytecode of this Python (%s) is not recognised: a "
                                  "*_instance field assigned by a base outside the project is "
                                  "found from every assignment in its methods, so a field of "
                                  "another object may be reported too", sys.version.split()[0])
        _STORE_PROBE.append(recognised)
    return _STORE_PROBE[0]


_UNKNOWN_OWNER = object()


def _owner_stores(function) -> set:
    """The *_instance stores whose object is the first parameter - loaded as a local (LOAD_FAST*)
    or as a cell a closure captures (LOAD_DEREF, LOAD_CLOSURE). A store on an object the scan
    names as another one (another variable, a global, an attribute) is not counted; one whose
    object it cannot name (a call result, ...) is counted: in doubt a field is reported, never
    lost."""
    code = function.__code__
    if not code.co_argcount:
        return set()
    first, names, owner = code.co_varnames[0], set(), _UNKNOWN_OWNER
    for instruction in dis.get_instructions(function):
        name = instruction.opname
        if name == "EXTENDED_ARG":
            continue  # a prefix of the next instruction
        if (name == "STORE_ATTR" and _DEF_ATTR_RE.match(instruction.argval)
                and (owner is _UNKNOWN_OWNER or owner == first)):
            names.add(instruction.argval)
        owner = _UNKNOWN_OWNER
        if name.startswith(("LOAD_FAST", "LOAD_DEREF", "LOAD_CLOSURE")):
            loaded = instruction.argval  # a pair is pushed left to right: the last one is on top
            owner = loaded[-1] if type(loaded) is tuple else loaded
        elif name.startswith(("LOAD_GLOBAL", "LOAD_NAME", "LOAD_ATTR")):
            owner = None  # another object, named
    return names


def _in_project(cls) -> bool:
    """True when ``cls``'s module is a file under a project root - auto_inject() patches the
    classes there. Plain values only: no code of the module or the class runs."""
    module_name = _TYPE_MODULE(cls)
    module = sys.modules.get(module_name) if type(module_name) is str else None
    if module is None or not any(klass is types.ModuleType for klass in _TYPE_MRO(type(module))):
        return False
    path = _MODULE_DICT(module).get("__file__")
    return type(path) is str and _is_late_root_path(path)


def _is_accessor(cls, name: str) -> bool:
    """True for a name that can never receive an injected value."""
    value = inspect.getattr_static(cls, name, None)
    if value is None or not hasattr(type(value), "__get__"):
        return False  # a plain class value: a field
    if isinstance(value, property):
        return value.fset is None  # read-only: an assignment would always fail
    # Methods (and other descriptors without __set__) are not fields; data
    # descriptors such as __slots__ members or attrs fields are.
    return not hasattr(type(value), "__set__")


def _patch_setattr(cls, container, name_map):
    """
    ORIGINAL FUNCTION - optimized internally with cache
    Eager injection via __setattr__.
    """
    # 🆕 v2 FIX: getattr yerine cls.__dict__ — bayrak base'den miras alınıp
    #            alt sınıfların sessizce atlanmasına neden oluyordu.
    if cls.__dict__.get("__ai_ioc_setattr__", False):
        return

    orig_setattr = getattr(cls, "__setattr__", object.__setattr__)
    compat_aliases = dict(cls.__dict__.get("__di_compat_aliases__", {}))


    def __setattr__(self, name, value):
        marker_name = compat_aliases.get(name)
        if marker_name is not None:
            # Keep old field names synchronized with the canonical *_instance
            # marker without changing the natural auto-injection path.
            setattr(self, marker_name, value)
            value = getattr(self, marker_name)
            return orig_setattr(self, name, value)
        if value is None and _DEF_ATTR_RE.match(name):
            normalized_base = _field_key(name)

            if DEBUG_MODE:
                logger.debug(f"Looking up {name!r} as {normalized_base!r} (class: {cls.__name__}, module: {cls.__module__})")

            resolution_container, reg = _lookup_registration(
                container, name_map, normalized_base, cls.__module__
            )

            if reg:
                try:
                    value = _resolve_via_container(
                        resolution_container, reg, isinstance(normalized_base, tuple)
                    )
                    if DEBUG_MODE:
                        impl = (reg.implementation_type or reg.service_type).__name__
                        logger.debug(f"{cls.__name__}.{name} <- {impl} (eager injection)")
                except Exception as e:
                    if DEBUG_MODE:
                        logger.debug(f"Injection failed: {e}")
                    raise AutoInjectionResolutionError(
                        f"{cls.__module__}.{cls.__name__}.{name}: "
                        f"{reg.service_type.__name__} could not be resolved"
                    ) from e
            else:
                _warn_unregistered_field(cls, name)

        return orig_setattr(self, name, value)

    cls.__setattr__ = __setattr__
    cls.__ai_ioc_setattr__ = True


def _warn_unregistered_field(cls, name: str) -> None:
    """A field with no matching registration stays None: one WARNING per field and reason, the same
    from the eager (__setattr__) and the post-init path, naming the real reason."""
    no_startup = _NAME_MAP_CACHE is None
    _warn_once(("unregistered-no-startup" if no_startup else "unregistered",
                cls.__module__, cls.__qualname__, name),  # each reason is reported once
               "%s.%s.%s: no matching registration; the field stays None%s",
               cls.__module__, cls.__qualname__, name,
               " (no container is active: the application's startup has not run)"
               if no_startup else "")


def _inject_fields(self, cls, container, name_map, di_candidates) -> None:
    """Post-init injection: fill the *_instance fields of one object that are still None."""
    # 🆕 v2: dir(self) yerine önceden hesaplanmış aday listesi
    for attr in di_candidates:
        try:
            val = getattr(self, attr)
        except Exception:
            logger.warning(
                "Field read failed for %s.%s.%s; field skipped (it keeps its current value)",
                cls.__module__, cls.__qualname__, attr, exc_info=True,
            )
            continue

        if val is not None:
            continue

        normalized_base = _field_key(attr)

        if DEBUG_MODE:
            logger.debug(f"Post-init lookup: {attr!r} as {normalized_base!r} (class: {cls.__name__}, module: {cls.__module__})")

        resolution_container, reg = _lookup_registration(
            container, name_map, normalized_base, cls.__module__
        )

        if not reg:
            _warn_unregistered_field(cls, attr)
            continue

        try:
            injected = _resolve_via_container(resolution_container, reg, isinstance(normalized_base, tuple))
            setattr(self, attr, injected)
            if DEBUG_MODE:
                impl = (reg.implementation_type or reg.service_type).__name__
                logger.debug(f"{cls.__name__}.{attr} <- {impl} (post-init injection)")
        except Exception as e:
            if DEBUG_MODE:
                logger.debug(f"Post-init injection failed: {e}")
            raise AutoInjectionResolutionError(
                f"{cls.__module__}.{cls.__name__}.{attr}: "
                f"{reg.service_type.__name__} could not be resolved"
            ) from e


def _patch_init(cls, container, name_map):
    """
    ORIGINAL FUNCTION - optimized internally with cache
    Post-init injection for remaining None attributes.
    """
    # 🆕 v2 FIX: getattr yerine cls.__dict__ — kalıtım bug'ı düzeltmesi
    _track(container, cls)  # bookkeeping never breaks patching, and never fails silently
    if cls.__dict__.get("__ai_ioc_init__", False):
        return

    orig_init = getattr(cls, "__init__", None)

    # 🆕 v2: Aday attribute'lar patch anında BİR KEZ hesaplanır.
    #        Çalışma anındaki pahalı dir() taraması (~2.6µs/nesne) kalkar.
    di_candidates = cls.__dict__.get("__di_attrs__")
    if di_candidates is None:
        di_candidates, own = _find_di_candidate_parts(cls)
        cls.__di_attrs__ = di_candidates
        cls.__di_own_attrs__ = own  # the container's "field injection only" measure

    def __init__(self, *args, **kwargs):
        # A non-callable __init__ fails exactly as it would without the patch.
        orig_init(self, *args, **kwargs)
        _inject_fields(self, cls, container, name_map, di_candidates)

    # 🆕 v2: __name__ kopyalamak yerine functools.wraps + imza koruması —
    #        FastAPI/pydantic gibi introspection yapan araçlar bozulmaz.
    if callable(orig_init):
        try:
            wraps(orig_init)(__init__)
        except Exception:
            __init__.__name__ = getattr(orig_init, "__name__", "__init__")
        try:
            __init__.__signature__ = _signature_of(orig_init, cls)
        except (ValueError, TypeError):
            pass
    else:
        __init__.__name__ = "__init__"
    cls.__init__ = __init__
    cls.__ai_ioc_init__ = True
    _PATCHED_IDS[id(cls)] = weakref.ref(cls, _forget_patched(id(cls)))


# ============================================================================
# MODULE DISCOVERY - FIXED!
# ============================================================================

def _find_project_root(start: Optional[Path] = None) -> Path:
    """
    🔧 FIX: Find the actual project root directory.
    Searches for common project markers, upwards from ``start`` or, when it is
    not given, from the __main__ module's directory.
    """
    # Start from __main__ module's directory if available
    main_mod = sys.modules.get("__main__")
    if start is not None:
        current = Path(start).resolve()
    elif main_mod and hasattr(main_mod, "__file__") and main_mod.__file__:
        current = Path(main_mod.__file__).resolve().parent
    else:
        current = Path.cwd()

    if DEBUG_MODE:
        logger.debug(f"Starting directory: {current}")

    # Common project root markers
    markers = [
        'requirements.txt',
        'setup.py',
        'pyproject.toml',
        'Pipfile',
        '.git',
        'main.py',
        'app.py',
    ]

    # Search up the directory tree
    checked_dirs = []
    for parent in [current] + list(current.parents):
        checked_dirs.append(str(parent))

        # Check for markers
        for marker in markers:
            if (parent / marker).exists():
                if DEBUG_MODE:
                    logger.debug(f"Project root found: {parent} (marker: {marker})")
                return parent

        # Don't go beyond home directory or root
        if parent == Path.home() or parent == parent.parent:
            break

    # If no markers found, use the starting directory
    if DEBUG_MODE:
        logger.debug(f"No project marker found; using starting directory: {current}")
        logger.debug(f"Directories checked: {checked_dirs[:3]}")

    return current


def _still_importing(error: BaseException) -> Optional[str]:
    """Name of the module that is still being imported and caused *error*.

    Discovery usually runs while the application module itself is being
    imported. A project file that imports that module back (an entry point
    such as ``main.py`` doing ``from app import app``) then fails with a
    circular import. That file is not broken: it simply cannot be loaded
    before the application module finishes. Returns None for real failures.
    """
    candidates = []
    name = getattr(error, "name", None)          # ImportError: ``from X import y``
    if isinstance(name, str):
        candidates.append(name)
    obj = getattr(error, "obj", None)            # AttributeError on a module object
    if isinstance(obj, type(sys)):
        candidates.append(obj.__name__)
    for candidate in candidates:
        module = sys.modules.get(candidate)
        spec = getattr(module, "__spec__", None)
        if getattr(spec, "_initializing", False):
            return candidate
    return None


# A module discovery skips because it imports one that is still being imported
# is not lost: it is listed, and checked the moment it loads (_check_deferred).
_DISCOVERING: list = []          # one list per running discovery (calls can nest)
_SKIPPED: Tuple[str, ...] = ()   # what the latest discovery skipped
_BLOCKERS: Dict[str, str] = {}   # ... and the module each of them waited for
_FAILED: Tuple[str, ...] = ()    # what the latest discovery could not import (issue texts)
_DEFERRED: Dict[str, bool] = {}  # skipped, not checked yet -> validate flag of its auto_inject()
_AWAITING: Dict[str, str] = {}   # skipped, not loaded yet -> the module it waits for


def _is_loading(module) -> bool:
    return bool(getattr(getattr(module, "__spec__", None), "_initializing", False))


def skipped_during_discovery() -> Tuple[str, ...]:
    """Modules the latest discovery could not load yet.

    Each one imports a module that was still being imported (typically an
    entry point such as ``main.py`` doing ``from app import app``). It is
    patched and checked the moment it loads; import it to see the result.
    """
    return _SKIPPED


def _import_module_safely(dotted_name: str, file_path: str) -> bool:
    """Import one discovered module; log why it was not loaded."""
    try:
        module = importlib.import_module(dotted_name)
        actual = getattr(module, "__file__", None)
        if not actual or Path(actual).resolve() != Path(file_path).resolve():
            logger.warning(
                "Module origin mismatch for %s: expected %s, imported %s",
                dotted_name, Path(file_path).resolve(), actual,
            )
            if _DISCOVERING:
                _DISCOVERING[-1]["failed"].append(
                    f"{dotted_name}: not loaded during discovery, another module with that name was imported ({actual})")
            return False
        return True
    except Exception as e:
        importing = _still_importing(e)
        if importing is not None:
            # Not an error yet: the module imports one that is still loading.
            # It is checked when it loads, so a real problem cannot hide here.
            logger.info(
                "Skipped %s during discovery: it imports %s, which is still being imported; "
                "it is checked when it loads",
                dotted_name, importing,
            )
            if _DISCOVERING:
                _DISCOVERING[-1]["skipped"].append((dotted_name, importing))
            return False
        logger.warning(f"Import failed for {dotted_name}: {e}", exc_info=True)
        if _DISCOVERING:
            _DISCOVERING[-1]["failed"].append(f"{dotted_name}: could not be imported during discovery ({e!r})")
        return False


def _normalize_excluded_dirs(exclude_dirs: Optional[Iterable[str]]) -> frozenset[str]:
    if exclude_dirs is None:
        return frozenset(_EXCLUDED_DIRS)
    if isinstance(exclude_dirs, (str, bytes)):
        raise TypeError("exclude_dirs must be an iterable of directory names")
    return frozenset(_EXCLUDED_DIRS).union(str(item) for item in exclude_dirs)


def _discover_and_load_modules(root: Path, exclude_dirs: Optional[Iterable[str]] = None) -> int:
    """Load the project's modules; remember the ones skipped as still loading."""
    global _SKIPPED, _BLOCKERS, _FAILED
    frame = {"skipped": [], "failed": []}
    _DISCOVERING.append(frame)
    try:
        return _load_project_modules(root, exclude_dirs)
    finally:
        _DISCOVERING.pop()
        _BLOCKERS = dict(frame["skipped"])
        _SKIPPED = tuple(sorted(_BLOCKERS))  # the same order on every file system
        _FAILED = tuple(frame["failed"])


def _load_project_modules(root: Path, exclude_dirs: Optional[Iterable[str]] = None) -> int:
    """
    🔧 FIXED: Discover and load all modules recursively.
    Now correctly handles nested directory structures.
    """
    excluded_dirs = _normalize_excluded_dirs(exclude_dirs)

    if DEBUG_MODE:
        logger.debug("Starting module discovery")
        logger.debug(f"Root directory: {root}")

    main_mod = sys.modules.get("__main__")
    main_path = Path(getattr(main_mod, "__file__", "")).resolve() if (main_mod and getattr(main_mod, "__file__", None)) else None

    # Get current script's path for exclusion
    current_script = Path(__file__).resolve()

    # Optimized: use Path.rglob
    py_files = list(root.rglob("*.py"))
    loaded = 0
    total_files = 0
    skipped = []

    for p in py_files:
        # 🆕 v2: Dışlanan dizinler (.venv, site-packages, __pycache__ vb.) —
        #        sanal ortamın binlerce dosyasının import edilmesini engeller.
        # Only folders INSIDE the project count: a project that itself lives
        # under e.g. ``.../examples/`` must still be discovered.
        try:
            inner_dirs = p.relative_to(root).parts[:-1]
        except ValueError:
            inner_dirs = p.parts
        if excluded_dirs.intersection(inner_dirs):
            continue

        # Skip conditions
        # Alt çizgiyle başlayan betikler (ör. kökteki geçici probe/script dosyaları)
        # DI keşfinde import edilmemeli; import yan etkisiyle uygulamayı kesebilirler.
        if (p.name == "__init__.py" or
            p.stem.startswith("_") or
            "auto_injector" in p.stem or
            "py_autowired" in p.stem or
            p == current_script or
            (main_path and p == main_path)):
            skipped.append((p.name, "system file"))
            continue

        total_files += 1

        try:
            rel = p.relative_to(root)
            dotted = ".".join(rel.with_suffix("").parts)

            if DEBUG_MODE:
                logger.debug(f"Loading: {dotted} ({rel})")

            if _import_module_safely(dotted, str(p)):
                loaded += 1
                if DEBUG_MODE:
                    logger.debug("Module loaded")
            else:
                skipped.append((dotted, "import failed"))

        except ValueError as e:
            # File is not relative to root
            if DEBUG_MODE:
                logger.debug(f"File outside root directory: {p}")
            skipped.append((str(p), "outside root"))
        except Exception as e:
            logger.warning("%s was not discovered: %s", p, e)
            skipped.append((str(p), f"error: {e}"))

    if DEBUG_MODE:
        logger.debug("Module loading summary:")
        logger.debug(f"Loaded: {loaded}/{total_files}")
        logger.debug(f"Skipped: {len(skipped)} files")
        if skipped and len(skipped) <= 5:
            for name, reason in skipped[:5]:
                logger.debug(f"  {name}: {reason}")

    return loaded


# ============================================================================
# MAIN FUNCTION - FIXED!
# ============================================================================

def _patch_module_classes(mod, container, name_map) -> int:
    """Patch the injection candidates of the classes defined in ``mod``."""
    patched = 0
    for cls_name, cls in inspect.getmembers(mod, inspect.isclass):
        if getattr(cls, "__module__", None) != mod.__name__:
            continue

        # 🆕 v2: SEÇİCİ PATCH — '*_instance' adayı olmayan sınıflar
        #        patch'lenmez → DI'sız sınıflarda setattr/__init__
        #        vergisi tamamen sıfırlanır.
        #        Eski davranış için: DI_PATCH_ALL=1
        if not PATCH_ALL_MODE:
            candidates = cls.__dict__.get("__di_attrs__")
            if candidates is None:
                candidates, own = _find_di_candidate_parts(cls)
                try:
                    cls.__di_attrs__ = candidates
                    cls.__di_own_attrs__ = own  # the container's "field injection only" measure
                except (TypeError, AttributeError):
                    pass  # bazı exotic tipler attribute kabul etmez
            if not candidates:
                # Include skipped classes in opt-in diagnostics without
                # installing any new injection wrapper or field candidate.
                _track(container, cls)
                if DEBUG_MODE:
                    logger.debug(f"Skipped {cls.__name__} (no injection candidates)")
                continue

        _patch_setattr(cls, container, name_map)
        _patch_init(cls, container, name_map)
        patched += 1

        if DEBUG_MODE:
            logger.debug(f"Patched {cls.__name__}")
    return patched


# ============================================================================
# MODULES IMPORTED AFTER auto_inject()
# ============================================================================
# Startup discovery imports the project once and skips some places on purpose
# (virtual environments, build copies, tests, ``_private.py`` files, ...). A
# project module that loads later - a lazy import, or a file from such a place -
# is patched the moment it is imported, so no ``*_instance`` field is missed
# because of where or when its module loads. Modules outside the project roots
# are never touched, and ordinary imports cost one prefix check.

_LATE_ROOTS: list[str] = []      # normalised project roots, each ending in a separator
_PENDING_LATE_MODULES: list = []  # imported while the global lock was busy
_STARTUP = threading.local()      # set while auto_inject() runs in this thread


def _is_late_root_path(path: str) -> bool:
    candidate = os.path.normcase(os.path.abspath(path))
    return any(candidate.startswith(root) for root in _LATE_ROOTS)


def _in_excluded_folder(path: str) -> bool:
    """True when a late-root file lies in a folder discovery skips (tests, venv, ...)."""
    candidate = os.path.normcase(os.path.abspath(path))
    for root in _LATE_ROOTS:
        if candidate.startswith(root):
            folders = candidate[len(root):].split(os.sep)[:-1]
            if _LATE_EXCLUDED.get(root, frozenset()).intersection(folders):
                return True
    return False


_LATE_EXCLUDED: Dict[str, frozenset] = {}  # root prefix -> folder names discovery skips there


def _remember_late_root(root: Path, excluded: Iterable[str] = ()) -> None:
    for form in (str(root), os.path.abspath(str(root))):
        prefix = os.path.normcase(form)
        if not prefix.endswith(os.sep):
            prefix += os.sep
        if prefix not in _LATE_ROOTS:
            _LATE_ROOTS.append(prefix)
        _LATE_EXCLUDED[prefix] = _LATE_EXCLUDED.get(prefix, frozenset()).union(excluded)
    if not any(isinstance(finder, _LateImportFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LateImportFinder())


def _check_deferred(module, container, name_map):
    """Check a module that discovery skipped, now that it has loaded.

    Its classes are patched (unless the import hook already did it) and get
    the static check of validate_autowiring(), so a wiring problem is reported
    now instead of when a field is first used. Returns the error if the
    auto_inject() call that skipped the module had validate=True.
    Caller holds the lock.
    """
    name = module.__name__
    if name not in _DEFERRED:
        return None
    strict = _DEFERRED.pop(name)
    try:
        _patch_module_classes(module, container, name_map)
        classes = [cls for _, cls in inspect.getmembers(module, inspect.isclass)
                   if getattr(cls, "__module__", None) == name]
        _validate_autowiring(container, classes)
    except AutowiringValidationError as error:
        failure = error
    except Exception as error:
        failure = AutowiringValidationError([f"{name}: could not be checked: {error!r}"])
        failure.__cause__ = error
        # Its classes may be untracked: validation must still report it later.
        _record_issue(container, failure.issues[0])
    else:
        logger.info("Checked %s after it loaded: no wiring problems", name)
        return None
    logger.error("%s has wiring problems now that it is loaded:\n%s",
                 name, "\n".join(f"- {issue}" for issue in failure.issues))
    return failure if strict else None


def _check_loaded_deferred(container, name_map=None):
    """Check skipped modules that loaded without passing the import hook.

    Returns the first error a validate=True caller asked for. Caller holds the lock.
    """
    failure = None
    for name in list(_DEFERRED):
        module = sys.modules.get(name)
        if module is None or _is_loading(module):
            continue
        if name_map is None:
            name_map = _build_name_map_from_container(container)
        error = _check_deferred(module, container, name_map)
        if failure is None:
            failure = error
    return failure


def _raise_first(failed: dict) -> None:
    """Raise the first validate=True failure, after the caller's own work is done."""
    for error in failed.values():
        raise error


def _load_skipped() -> None:
    """Import skipped modules whose blocking module has finished loading.

    Runs at the first injection, refresh(), activate_container() or
    validate_autowiring() after that point, so a skipped file is checked even
    when the application never imports it. Discovery imports every project
    file anyway; this is the same import, only later. The import itself runs
    outside the lock; the import hook patches and checks the module.
    """
    ready = [name for name, blocker in list(_AWAITING.items()) if _has_loaded(blocker)]  # lock-free
    if not ready:
        return
    with _GLOBAL_LOCK:
        # Another thread may have taken some of them meanwhile: keep only ours.
        ready = [name for name in ready if _AWAITING.pop(name, None) is not None]
    failure = None
    for name in ready:
        if name in sys.modules:
            continue  # the application imported it: already checked on load
        try:
            importlib.import_module(name)
        except AutowiringValidationError as error:
            failure = failure or error
        except Exception as error:
            issue = f"{name}: could not be imported after startup ({error!r})"
            # This may run inside a resolution: the background writer logs it.
            _BACKGROUND_LOG.put("Import failed for %s: %s", (name, error), sys.exc_info())
            cache = _NAME_MAP_CACHE
            if cache is not None:
                _record_issue(cache._container, issue)
            if _DEFERRED.get(name):  # validate=True: an exception, not only a log line
                failure = failure or AutowiringValidationError([issue])
    with _GLOBAL_LOCK:
        cache = _NAME_MAP_CACHE
        if cache is not None:
            # Loaded without passing the hook (e.g. during another auto_inject()).
            failure = failure or _check_loaded_deferred(cache._container)
    if failure is not None:
        raise failure


def _has_loaded(name: str) -> bool:
    module = sys.modules.get(name)
    return module is not None and not _is_loading(module)


def _drain_late_modules() -> dict:
    """Patch modules that were imported while the lock was busy. Caller holds the lock.

    Returns {module name: error} for skipped modules whose check failed with validate=True.
    """
    cache = _NAME_MAP_CACHE
    if cache is None:
        return {}
    name_map = cache.get_raw_map()
    failed = {}
    while _PENDING_LATE_MODULES:
        try:
            module = _PENDING_LATE_MODULES.pop()
        except IndexError:
            break
        try:
            _patch_module_classes(module, cache._container, name_map)
        except Exception as error:
            # Its classes stay unpatched (their fields stay None): an error, and a
            # validation issue, not a passing note.
            logger.error("Could not patch late-imported module %s", module.__name__, exc_info=True)
            _record_issue(cache._container, f"{module.__name__}: could not be patched ({error!r})")
        if module.__name__ not in _DEFERRED and _is_validating(cache._container):
            _DEFERRED[module.__name__] = True  # validate=True: every late module is checked
        error = _check_deferred(module, cache._container, name_map)
        if error is not None:
            failed[module.__name__] = error
            # Its importer may be gone (queued import) or may retry: keep it
            # pending, so the next import, auto_inject() or validate_autowiring()
            # checks it again and raises.
            _DEFERRED[module.__name__] = True
    return failed


_VALIDATING = weakref.WeakSet()  # containers whose auto_inject() had validate=True
# The same for a container that cannot be weakly referenced: kept alive (as in _PINNED),
# so its id is never given to another object.
_VALIDATING_PINNED: Dict[int, Any] = {}


def _is_validating(container) -> bool:
    if id(container) in _VALIDATING_PINNED:
        return True
    try:
        return container in _VALIDATING
    except TypeError:  # an unhashable container: it is in _VALIDATING_PINNED if validating
        return False


def _patch_late_module(module) -> None:
    if getattr(_STARTUP, "active", False):
        return  # auto_inject() patches (and checks) every module it loads itself
    # A bounded wait: another thread may hold the lock while it waits for this
    # very import; the module is then queued for the next lock holder.
    if not _GLOBAL_LOCK.acquire(timeout=0.5):
        _PENDING_LATE_MODULES.append(module)
        _warn_once_background(
            ("queued", module.__name__),
            "%s was imported while py_autowired was busy; its classes are patched as soon as "
            "the lock is free - objects created before that get no injection", module.__name__)
        # No other lock holder may ever come: finish it as soon as the lock is free.
        threading.Thread(target=_drain_when_free, name="py_autowired-late-import", daemon=True).start()
        return
    try:
        _PENDING_LATE_MODULES.append(module)
        error = _drain_late_modules().get(module.__name__)
    finally:
        _GLOBAL_LOCK.release()
    if error is not None:
        raise error  # validate=True: this import fails, like a failed startup check


def _drain_when_free() -> None:
    """Patch (and check) queued late imports once the lock is free. Problems are
    logged; a validate=True failure stays pending for the next library call."""
    with _GLOBAL_LOCK:
        _drain_late_modules()


class _LateImportLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module) -> None:
        super().exec_module(module)
        _patch_late_module(module)


def _under_a_root(directory: str) -> bool:
    candidate = os.path.normcase(os.path.abspath(directory or ".")) + os.sep
    return any(candidate.startswith(root) for root in _LATE_ROOTS)


def _root_heads(directory: str) -> frozenset:
    """For a directory ABOVE a root: the root's first part below it - the top-level name under
    which the root itself (a package directory) is imported from that sys.path entry."""
    prefix = os.path.normcase(os.path.abspath(directory or ".")).rstrip(os.sep) + os.sep
    return frozenset(root[len(prefix):].split(os.sep, 1)[0] for root in _LATE_ROOTS
                     if root.startswith(prefix) and len(root) > len(prefix))


def _maybe_project_name(fullname: str, path) -> bool:
    """False only when ``fullname`` cannot be a module under a project root: its top-level name is
    not a root's own name seen from a sys.path entry above it, Python's own path search finds it in
    no sys.path entry under a root, and (for a submodule) no directory of its parent's path is under
    a root. Python's search shares Python's own directory caches, so it answers exactly as the
    import that follows will (no cache of ours can say "not there"); for a missing name it costs one
    stat per entry under a root, not a scan of all of sys.path. Every doubt goes the full way."""
    if path is not None:
        for directory in list(path):
            if isinstance(directory, str) and _under_a_root(directory):
                return True
    top = fullname.partition(".")[0]
    _seen, under, heads, relative = _root_entries()
    folded = os.path.normcase(top)
    if folded in heads:
        return True
    entries = list(under)
    for entry in relative:  # "" or a relative path: the current directory decides, every time
        if folded in _root_heads(entry):
            return True
        if _under_a_root(entry):
            entries.append(entry)
    try:
        if entries and importlib.machinery.PathFinder.find_spec(top, entries) is not None:
            return True
        # Any other finder (an editable install's name map, ...) may lead into a root without a
        # sys.path entry: asked as before - usually a dictionary look-up, not a path scan.
        for finder in list(sys.meta_path):
            if type(finder) is _LateImportFinder or finder in _STANDARD_FINDERS:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is not None and find_spec(fullname, path, None) is not None:
                return True
    except Exception:  # a broken hook: not ours to judge
        return True
    return False


_STANDARD_FINDERS = (importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter,
                     importlib.machinery.PathFinder)


# (sys.path and roots seen, entries under a root, root names seen from entries above a root,
# relative entries). ONE immutable tuple, replaced by one assignment: a reader never mixes two
# states (free-threaded build). Relative entries are never resolved here (the current directory may
# change without sys.path changing). Entries keep their sys.path spelling, so Python's search uses
# the very finders - and directory caches - the import itself uses.
_ROOT_ENTRIES: tuple = ((), (), frozenset(), ())


def _root_entries() -> tuple:
    global _ROOT_ENTRIES
    key = (tuple(sys.path), tuple(_LATE_ROOTS))
    state = _ROOT_ENTRIES
    if state[0] == key:
        return state
    under, heads, relative = [], set(), []
    for entry in key[0]:
        if not isinstance(entry, str):  # as Python's own path search
            continue
        if not entry or not os.path.isabs(entry):
            relative.append(entry)
            continue
        if _under_a_root(entry):
            under.append(entry)
        heads |= _root_heads(entry)
    state = (key, tuple(under), frozenset(heads), tuple(relative))
    _ROOT_ENTRIES = state
    return state


class _LateImportFinder(importlib.abc.MetaPathFinder):
    """Asks the other finders and marks project source modules for patching. A name that cannot
    be a project module is left to them at once - asking them here too searched every failing
    import twice (0.5.4: one optional import per request cost about 0.5 ms)."""

    def find_spec(self, fullname, path, target=None):
        if not _LATE_ROOTS or not _maybe_project_name(fullname, path):
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if spec.origin and _is_late_root_path(spec.origin):
            if type(loader) is importlib.machinery.SourceFileLoader:
                spec.loader = _LateImportLoader(loader.name, loader.path)
            elif spec.origin.endswith((".py", ".pyc")) and not _in_excluded_folder(spec.origin):
                # Behaviour is unchanged (the module is not patched), but never silently.
                _warn_once(("loader", fullname),
                           "%s is imported by %s, which py_autowired cannot hook; its classes are "
                           "not patched", fullname, type(loader).__name__)
        return spec


def _startup_patching(func):
    """Mark auto_inject() as running, then catch up on queued late imports."""
    @wraps(func)
    def run(*args, **kwargs):
        previous = getattr(_STARTUP, "active", False)
        _STARTUP.active = True
        try:
            result = func(*args, **kwargs)
        finally:
            _STARTUP.active = previous
        _raise_first(_drain_late_modules())
        return result

    return run


def _serialized_class_patching(func):
    """Serialize global class/cache mutation without changing the public API."""
    @wraps(func)
    def locked(*args, **kwargs):
        with _GLOBAL_LOCK:
            _GLOBAL_LOCK.let_go(_NAME_MAP_CACHE)  # the active map it may replace: let go after the lock
            return func(*args, **kwargs)

    return locked


@_serialized_class_patching
@_startup_patching
def auto_inject(
    container,
    root_dir: str | None = None,
    *,
    exclude_dirs: Optional[Iterable[str]] = None,
    validate: bool = False,
):
    """
    🔧 FIXED VERSION - Now correctly handles nested structures!

    ⚠️ CRITICAL: Call ONCE at application startup!
    Never call in request handlers!

    Args:
        container: DI container with service registrations
        root_dir: Optional custom root directory (if None, auto-detects project root)

        exclude_dirs: Additional directory names that discovery must skip.
            Built-in safety exclusions remain active.
        validate: Inspect patched fields without constructing services and raise
            one AutowiringValidationError containing all static problems.
            A module skipped during discovery (it imports a module that is
            still being imported) is checked when it loads; with validate=True
            a problem found then makes that import fail.
    Returns:
        Number of patched classes
    """
    if DEBUG_MODE:
        logger.debug("Starting automatic injection")
        logger.debug("=" * 60)

    # 🔧 FIX: Use project root finder instead of __file__
    if root_dir is None:
        root = _find_project_root()
    else:
        root = Path(root_dir).resolve()

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _remember_late_root(root, _normalize_excluded_dirs(exclude_dirs))

    if DEBUG_MODE:
        logger.debug(f"Root directory: {root}")
        logger.debug(f"sys.path[0]: {sys.path[0]}")

    # Load modules
    if exclude_dirs is None:
        loaded_count = _discover_and_load_modules(root)
    else:
        loaded_count = _discover_and_load_modules(root, exclude_dirs)
    try:
        (_VALIDATING.add if validate else _VALIDATING.discard)(container)
    except TypeError:  # it cannot be weakly referenced
        if validate:
            _VALIDATING_PINNED[id(container)] = container
        else:
            _VALIDATING_PINNED.pop(id(container), None)
    for name in _SKIPPED:
        _DEFERRED[name] = validate
        _AWAITING[name] = _BLOCKERS[name]
    for text in _FAILED:
        _record_issue(container, text)  # discovery was incomplete: validation says so

    # Interfaces registered before their implementation module was imported
    # get it now, so the name map and validation see it.
    complete_implementations = getattr(container, "_complete_implementations", None)
    if complete_implementations is not None:
        complete_implementations()
    _warn_missing_implementations(container)

    # Build cached name map
    global _NAME_MAP_CACHE
    _NAME_MAP_CACHE = _CachedNameMap(container)
    name_map = _NAME_MAP_CACHE.get_raw_map()

    # Patch classes
    if DEBUG_MODE:
        logger.debug("Patching classes")

    patched = 0

    for mod_name, mod in list(sys.modules.items()):
        if not hasattr(mod, "__file__") or not mod.__file__:
            continue

        try:
            mod_path = Path(mod.__file__).resolve()
        except Exception as error:
            logger.warning("The path of module %s cannot be read (%r); if it belongs to the "
                           "project, its classes are not patched", mod_name, error)
            continue
        if not _is_rel_to(mod_path, root):
            continue

        if DEBUG_MODE:
            mod_info = _get_module_depth_info(mod_name)
            logger.debug(f"Module: {mod_name} (depth: {mod_info['depth']})")

        patched += _patch_module_classes(mod, container, name_map)

    if DEBUG_MODE:
        logger.debug("Autowiring complete")
        logger.debug(f"Loaded {loaded_count} modules; patched {patched} classes")
        logger.debug("=" * 60)
    if loaded_count == 0 and patched == 0:
        logger.warning("Nothing to inject: no project module was loaded from %s and no class "
                       "was patched; check root_dir", root)
    _warn_equal_candidates(container, name_map)

    # A skipped module that loaded while this call ran (during its discovery)
    # did not pass through the import hook: check it now.
    failure = _check_loaded_deferred(container, name_map)
    if validate:
        validate_autowiring(container)
    if failure is not None:
        raise failure
    return patched


def _field_candidates(name_map, name):
    """Registrations a field can receive, narrowed like runtime selection."""
    unique = {}
    for reg in name_map.get(_field_key(name), ()):
        # Alias/index duplicates are not ambiguity, and neither are two
        # registrations of the SAME ready object (register_instance(A, obj) +
        # register_instance(B, obj)): every candidate gives the field that object.
        target = ("object", id(reg.instance)) if getattr(reg, "has_instance", False) else id(reg)
        unique.setdefault(target, reg)
    regs = list(unique.values())
    primaries = [reg for reg in regs if getattr(reg, "primary", False)]
    return primaries or regs


def _tied_candidates(regs, module):
    best = max(_registration_score(reg, module) for reg in regs)
    return [reg for reg in regs if _registration_score(reg, module) == best]


def _registration_label(reg) -> str:
    target = reg.implementation_type or reg.service_type
    return f"{target.__module__}.{target.__qualname__}"


_UNSET = object()


def _open_fields(cls):
    """Fields of ``cls`` that injection fills (a concrete class value satisfies one)."""
    for name in cls.__dict__.get("__di_attrs__", ()):
        value = inspect.getattr_static(cls, name, _UNSET)
        # Do not invoke descriptors; constructor assignments cannot be known statically.
        if value is not _UNSET and value is not None and not inspect.isdatadescriptor(value):
            continue
        yield name


_INJECTED_FIELDS = weakref.WeakKeyDictionary()  # class -> its *_instance fields, for injected()
_REGISTERED_REPORTED = weakref.WeakKeyDictionary()  # registered class -> {reason: fields reported}
# The classes the library patched, kept by the library itself: id -> weak reference. Asking it
# needs no dictionary of the class (int keys: no user __hash__/__eq__).
_PATCHED_IDS: Dict[int, Any] = {}


def _forget_patched(key: int):
    def forget(ref) -> None:  # the class is gone: drop its entry (no lock; a dict op)
        if _PATCHED_IDS.get(key) is ref:
            del _PATCHED_IDS[key]
    return forget
# A class's own namespace, MRO and names, read through type's own descriptors (as
# inspect.getattr_static does): a metaclass that redefines them runs no code here.
_TYPE_DICT = type.__dict__["__dict__"].__get__
_TYPE_MRO = type.__dict__["__mro__"].__get__
_TYPE_MODULE = type.__dict__["__module__"].__get__
_TYPE_QUALNAME = type.__dict__["__qualname__"].__get__
_MODULE_DICT = types.ModuleType.__dict__["__dict__"].__get__


def _inject_unpatched(instance, container) -> bool:
    """injected() built an object of a class discovery never patched (outside the
    project root, or created after startup): fill the None fields of that ONE
    object, the way a patched __init__ does. The class is not changed - modules
    outside the project are never touched - so a field used inside __init__ right
    after ``= None`` is not filled yet there. The caller checked that no other
    container is the active one (_check_active); with none active (startup has not
    run) a field that is not found stays None with a WARNING naming it, as in 0.5.3. True when the
    class is patched (nothing to do here, ever)."""
    cls = type(instance)
    candidates = _unpatched_candidates(cls)
    if candidates is None:
        return True
    _inject_fields(instance, cls, container, {}, candidates)
    return False


def _unpatched_candidates(cls):
    """The *_instance fields of a class discovery never patched (found once per class); None
    for a patched class - its patched __init__ handles its fields."""
    if cls.__dict__.get("__ai_ioc_init__", False):
        return None  # its patched __init__ has filled it
    try:
        return _INJECTED_FIELDS[cls]
    except KeyError:
        candidates = _INJECTED_FIELDS[cls] = _find_di_candidates(cls)
        return candidates


def _warn_unfilled_registered(instance) -> bool:
    """injected() got a registered type whose class discovery never patched (startup has not run,
    the class is outside the project root, was created after startup or showed no field when it
    was scanned). Its None fields are NOT
    filled from outside the container: a shared singleton would keep the first request's scoped
    objects. One WARNING per field (the same once-only rule as every field WARNING) says so.

    Only PLAIN values are read: the object's own dictionary when it is a plain dict from Python's
    own __dict__, plain class values and slots - no code of the object, its class or its metaclass
    runs. Anything else (the class defines its own __dict__, the dictionary is a dict subclass, a
    field-like name is a descriptor) is one WARNING per class and reason: its fields cannot be
    checked - never silence. Nothing here may change what injected() returns: a failure of the check
    itself is one WARNING. True when the library patched the class (nothing to check, ever)."""
    cls = type(instance)
    patched = _PATCHED_IDS.get(id(cls))
    if patched is not None and patched() is cls:
        return True  # the library patched it: its own __init__ handles its fields (nothing is read)
    module, name = _TYPE_MODULE(cls), _TYPE_QUALNAME(cls)
    try:
        # Only plain snapshots are looked at from here on (_plain_snapshot): never a live mapping
        # twice, never a key that is not exactly a str.
        merged, unchecked = _class_chain(cls)
        if unchecked is None:
            own, unchecked = _plain_fields_source(instance, cls, merged)
        if unchecked is not None:
            _warn_once(("registered-unchecked", module, name, unchecked),
                       "%s.%s: its fields cannot be checked (%s) - injected() reads only plain "
                       "values, so it cannot see whether one is left None",
                       module, name, unchecked)
            return
        no_startup = _NAME_MAP_CACHE is None
        # Each field is reported once per reason; a field already reported is not read again.
        reported = _REGISTERED_REPORTED.setdefault(cls, {}).setdefault(no_startup, set())
        for attr in _unfilled_fields(instance, cls, merged, own, reported):
            _warn_once(("registered-unpatched-no-startup" if no_startup else "registered-unpatched",
                        module, name, attr),
                       "%s.%s.%s: the field stays None - a registered type's fields are not filled "
                       "from outside the container (%s); move the class inside the project root or "
                       "use constructor injection",
                       module, name, attr,
                       "no container is active: the application's startup has not run" if no_startup
                       else "its class is in the project but was never patched: no *_instance "
                            "field was found in it when it was scanned, or it was created after "
                            "startup" if _in_project(cls)
                       else "its class was never patched: it is outside the project root")
            reported.add(attr)
    except Exception as error:
        try:
            _warn_once(("registered-unpatched-check", module, name),
                       "%s.%s: the check for fields left None failed (%r); the object is returned "
                       "as built", module, name, error)
        except Exception:
            pass  # the log itself fails: the object is still returned as built


def _plain_snapshot(mapping):
    """A copy of ``mapping`` (an exact dict, or a class's mappingproxy) when every key is exactly a
    str; otherwise None. Its items are taken in ONE pass - walking runs no __hash__ or __eq__ - and
    the copy is built only from verified str keys (str hashing and equality are C code). After it
    the check looks only at copies, never at the live mapping again: no race with a thread that
    changes it."""
    items = list(mapping.items())
    for key, _ in items:
        if type(key) is not str:
            # The list may hold a value's last reference (the mapping changed meanwhile, or it
            # was built on the fly): dropped after the library lock - its __del__ is user code.
            _GLOBAL_LOCK.let_go(items)
            return None
    return dict(items)


def _class_chain(cls) -> tuple:
    """(its MRO's plain snapshots merged - the first class wins, as attribute lookup does -, None)
    or (None, why not)."""
    merged = {}
    for klass in reversed(_TYPE_MRO(cls)):
        snapshot = _plain_snapshot(_TYPE_DICT(klass))
        if snapshot is None:
            return None, f"the __dict__ of {_TYPE_QUALNAME(klass)} has a key that is not a plain str"
        merged.update(snapshot)
    return merged, None


def _plain_fields_source(instance, cls, merged) -> tuple:
    """(a plain snapshot of the object's own dictionary, None) when its fields can be read as plain
    values without running any code; otherwise (None, why they cannot be checked)."""
    found = merged.get("__dict__", _UNSET)
    if found is _UNSET:
        own = {}  # no __dict__ at all: slots only
    elif type(found) is not types.GetSetDescriptorType:
        return None, "the class defines its own __dict__"
    else:
        try:  # the checked getset itself (C code): no second live lookup of __dict__
            live = found.__get__(instance, cls)
        except AttributeError:
            live = {}
        if type(live) is not dict:
            return None, "its __dict__ is not a plain dict"
        own = _plain_snapshot(live)
        if own is None:
            return None, "its __dict__ has a key that is not a plain str"
    copies = {}  # this check's copies of the value types' dictionaries (never kept across checks)
    for field in sorted(name for name in merged if _DEF_ATTR_RE.match(name)):
        value = merged[field]
        if type(value) is types.MemberDescriptorType:
            continue  # a slot: read by C code
        kind = _is_descriptor(value, copies)
        if kind is None:
            return None, f"the type of {field}'s value has a key that is not a plain str"
        if kind:
            return None, f"{field} is a descriptor, not a plain value"
    return own, None


def _unfilled_fields(instance, cls, merged, own, skip=frozenset()) -> list:
    """The *_instance fields that hold None - plain values only (see _plain_fields_source); the
    names in ``skip`` are not read. Every name is read every time: a field the class gets later is
    seen too (only the values of reported fields are not read again)."""
    names = {name for name in (*own, *merged) if _DEF_ATTR_RE.match(name)}  # plain str keys only
    return [name for name in sorted(names - skip) if _field_value(instance, cls, merged, name, own) is None]


def _is_descriptor(value, copies):
    """True when ``value``'s type defines __get__, __set__ or __delete__; None when one of that
    type's dictionaries has a key that is not a plain str (it is not looked into). ``copies``:
    id(class) -> (class, copy) of this one check."""
    for klass in _TYPE_MRO(type(value)):
        cached = copies.get(id(klass))
        if cached is not None and cached[0] is klass:
            snapshot = cached[1]
        else:
            snapshot = _plain_snapshot(_TYPE_DICT(klass))
            copies[id(klass)] = (klass, snapshot)
        if snapshot is None:
            return None
        if "__get__" in snapshot or "__set__" in snapshot or "__delete__" in snapshot:
            return True
    return False


def _field_value(instance, cls, merged, name, own):
    """One plain field's value: a slot (C read), else the object's own value, else the class
    value; _UNSET: none. Only called after _plain_fields_source said the values are plain."""
    in_class = merged.get(name, _UNSET)
    if type(in_class) is types.MemberDescriptorType:  # a slot: the C-level read runs no user code
        try:
            return in_class.__get__(instance, cls)
        except AttributeError:  # never set (no __getattr__ is asked)
            return _UNSET
    return own.get(name, in_class)  # the object's own value comes before the class value


def _check_active(container) -> None:
    """A request of ``container``'s application (web bridges, injected()): if another
    container is the active one, fail before anything is built - patched classes would
    be filled from it. A best-effort catch, not a guarantee: one container per process
    is supported (several applications in one process: 0.6.0). No active container at
    all (startup has not run) is 0.5.3's behaviour."""
    cache = _NAME_MAP_CACHE
    if cache is not None and cache._container is not container:
        raise DependencyInjectionError(
            "This request's container is not the active one: another application's container "
            "was activated in this process. py_autowired supports one container per process: "
            "run each application in its own process. In tests: open each FastAPI application "
            "with `with TestClient(app)` (its startup makes its container the active one); with "
            "Flask or Django, or anywhere, call activate_container(container) before using "
            "another application")


def _warn_equal_candidates(container, name_map) -> None:
    """ONE startup WARNING listing every field whose best candidates tie.

    Runtime then takes the first registered one; validate=True reports the same
    fields as errors. Grouped so a project with many ties gets one entry.
    """
    with _GLOBAL_LOCK:
        classes = tuple(_PATCHED_CLASSES.get(id(container), {}).values())
    lines = []
    for cls in sorted(classes, key=lambda c: (c.__module__, c.__qualname__)):
        for name in _open_fields(cls):
            regs = _field_candidates(name_map, name)
            if len(regs) < 2:
                continue
            tied = _tied_candidates(regs, cls.__module__)
            if len(tied) > 1:
                lines.append(f"{cls.__module__}.{cls.__qualname__}.{name}: "
                             + ", ".join(_registration_label(reg) for reg in tied))
    if not lines:
        return
    try:
        if _REPORTED_TIES.get(container) == lines:
            return  # already reported (auto_inject() and refresh() call this)
        _REPORTED_TIES[container] = lines
    except TypeError:
        pass
    logger.warning("%d fields have equally good candidates; the first registered one is used:\n%s",
                   len(lines), "\n".join(f"- {line}" for line in lines))


_REPORTED_TIES = weakref.WeakKeyDictionary()  # container -> tie lines last reported


def _warn_missing_implementations(container) -> None:
    """A contract still without an implementation after discovery is a WARNING now,
    not only an error at its first resolve()."""
    seen = set()
    for reg in (*getattr(container, "registrations", {}).values(),
                *getattr(container, "_keyed", {}).values()):
        if id(reg) in seen:
            continue
        seen.add(id(reg))
        if (getattr(reg, "implementation_type", None) is None
                and getattr(reg, "factory", None) is None
                and not getattr(reg, "has_instance", False)
                and inspect.isabstract(reg.service_type)):
            logger.warning("%s.%s is registered with no implementation and none was found in the "
                           "project; resolve() looks again and fails if there is still none",
                           reg.service_type.__module__, reg.service_type.__qualname__)


def validate_autowiring(container) -> None:
    """Startup checks for this container's patched classes.

    The check does not construct objects/services, call factories or field
    descriptors, or open scopes. It may evaluate type annotations, which can
    execute user code. It cannot predict arbitrary constructor/factory behavior.
    A module skipped during discovery that has loaded since is patched and
    checked first, and one whose blocking module has finished is imported now;
    one that still cannot load is named on a line of its own.
    """
    _load_skipped()
    with _GLOBAL_LOCK:
        # A skipped module may have loaded around the import hook: check it first.
        _check_loaded_deferred(container)
        _forget_collected()  # a collected container's list must not pass for this one's
        classes = tuple(_PATCHED_CLASSES.get(id(container), {}).values())
        skipped = tuple(_DEFERRED)
        untracked = tuple(_UNTRACKED.get(id(container), ()))
    # Run outside the global lock: validation may evaluate annotations (imports)
    # and log; neither must happen while patching/refresh could be waiting.
    _validate_autowiring(container, classes, skipped, untracked)


# ============================================================================
# 🆕 v2: RUNTIME REFRESH - Geç kayıtlar için
# ============================================================================

def refresh():
    """
    🆕 v2: Container'a auto_inject'ten SONRA yeni servis kaydedildiyse
    isim haritasını yeniden kurar ve çözümleme cache'ini temizler.

    Kullanım (örn. plugin senaryosu):
        container.register_singleton(ABS_YeniServis)
        py_autowired.refresh()

    Not: Bu fonksiyon yeni SINIF patch'lemez; yalnızca mevcut patch'li
    sınıfların yeni kayıtları görmesini sağlar. Yeni modül/sınıf eklediyseniz
    auto_inject()'i yeniden çağırın (idempotenttir, patch'liler atlanır).
    """
    global _NAME_MAP_CACHE
    with _GLOBAL_LOCK:
        if _NAME_MAP_CACHE is None:
            return False
        _NAME_MAP_CACHE.refresh()
        failed = _drain_late_modules()
        cache = _NAME_MAP_CACHE
        if DEBUG_MODE:
            logger.debug("Name map refreshed; resolution cache cleared")
    _warn_equal_candidates(cache._container, cache.get_raw_map())  # new registrations may tie
    # The refresh is complete; now report what the drained imports found.
    _raise_first(failed)
    _load_skipped()
    return True


def activate_container(container) -> None:
    """Make an already-installed container the active legacy resolution graph.

    Class patches are process-global, while tests and controlled runtime rebuilds
    may create more than one container. Calling ``auto_inject`` for another
    container must therefore not permanently poison a cached application's
    name map. Activation only swaps the lookup owner under the same global lock;
    it neither discovers modules nor patches classes again.
    """
    global _NAME_MAP_CACHE
    with _GLOBAL_LOCK:
        if _NAME_MAP_CACHE is None or _NAME_MAP_CACHE._container is not container:
            _GLOBAL_LOCK.let_go(_NAME_MAP_CACHE)  # the replaced map: let go after the lock
            _NAME_MAP_CACHE = _CachedNameMap(container)
        failed = _drain_late_modules()
    # The container is active; now report what the drained imports found.
    _raise_first(failed)
    _load_skipped()


def check_skipped(container=None) -> Tuple[str, ...]:
    """Import and check every module the latest discovery skipped.

    For tests and startup checks. Unlike the automatic check it raises
    AutowiringValidationError for any problem whatever ``validate`` was, and it
    also reports a module that was already checked (and logged) earlier.
    ``container`` defaults to the active one. Returns the checked module names.
    """
    if container is None:
        cache = _NAME_MAP_CACHE
        if cache is None:
            raise DependencyInjectionError("check_skipped() needs a container: auto_inject() has not run")
        container = cache._container
    names = _SKIPPED
    issues = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except AutowiringValidationError as error:
            issues.extend(error.issues)
            continue
        except Exception as error:
            issues.append(f"{name}: could not be imported: {error!r}")
            continue
        if _is_loading(module):
            issues.append(f"{name}: is still being imported; call check_skipped() after startup")
            continue
        try:
            with _GLOBAL_LOCK:
                name_map = _build_name_map_from_container(container)
                _check_deferred(module, container, name_map)  # logs, if still pending
                _patch_module_classes(module, container, name_map)
            _validate_autowiring(container, [
                cls for _, cls in inspect.getmembers(module, inspect.isclass)
                if getattr(cls, "__module__", None) == name])
        except AutowiringValidationError as error:
            issues.extend(error.issues)
        except Exception as error:
            issues.append(f"{name}: could not be checked: {error!r}")
    if issues:
        raise AutowiringValidationError(issues)
    return names

# ============================================================================
# BONUS: Performance monitoring utilities
# ============================================================================

def get_cache_stats() -> dict:
    """Get cache statistics - NEW utility function"""
    return {
        'norm_cache': _norm.cache_info()._asdict(),
        'similarity_cache': _calculate_module_similarity.cache_info()._asdict(),
        'resolution_cache_size': len(_NAME_MAP_CACHE._resolution_cache) if _NAME_MAP_CACHE else 0
    }


def print_cache_stats():
    """Print cache statistics - NEW utility function"""
    stats = get_cache_stats()
    print("\n📊 Cache Statistics:")
    print(f"  _norm: hits={stats['norm_cache']['hits']}, misses={stats['norm_cache']['misses']}")
    print(f"  _calculate_module_similarity: hits={stats['similarity_cache']['hits']}, misses={stats['similarity_cache']['misses']}")
    print(f"  Resolution cache entries: {stats['resolution_cache_size']}")



# Opt-in static analysis; deliberately outside the runtime resolution path.

def _validate_autowiring(container, classes, skipped=(), untracked=()):
    # Build a fresh view: validation neither reads nor refreshes the runtime cache.
    registrations = dict(container.registrations)  # snapshot; no nested locks
    earlier = {st: list(regs) for st, regs in getattr(container, "_earlier", {}).items()}
    name_map = _build_name_map_from_container(container)

    def targets(regs, name):
        # A plural field receives every registration of the matched contract.
        if not name.endswith("_instances"):
            return regs
        return [item for reg in regs for item in (*earlier.get(reg.service_type, ()), reg)]

    fields = _open_fields

    def candidates(cls, name):
        return _field_candidates(name_map, name)

    def construction_problems(reg, path=(), singleton_owner=None, lifetime_only=False):
        service = reg.service_type
        if reg.scope == LifetimeScope.SINGLETON:
            singleton_owner = singleton_owner or service
            lifetime_only = lifetime_only or reg.has_instance
        if reg.scope == LifetimeScope.SCOPED and singleton_owner is not None:
            return [f"singleton {singleton_owner.__name__} captures scoped {service.__name__}"]
        ident = getattr(reg, "slot", service)  # (type, key) for keyed registrations
        if ident in path:
            if lifetime_only:
                return []
            return ["circular dependency: " + " -> ".join(_slot_name(t) for t in path + (ident,))]
        path = path + (ident,)
        if reg.factory is not None:
            problems = []
            # Factory bodies remain opaque; only explicit metadata can be read.
            for dependency in getattr(reg.factory, "__di_dependencies__", ()):
                child = registrations.get(dependency)
                if child is None and not lifetime_only:
                    problems.append(f"factory requires unregistered {dependency!r}")
                elif child is not None:
                    problems.extend(construction_problems(child, path, singleton_owner, lifetime_only))
            return problems
        impl = reg.implementation_type
        if impl is None:
            return [] if lifetime_only else [f"{service.__name__} has no implementation"]
        if inspect.isabstract(impl) and not lifetime_only:
            return [f"{impl.__name__} is abstract"]
        try:
            signature = _init_signature(impl)
        except (TypeError, ValueError) as error:
            if lifetime_only:
                return []
            if _field_injection_only(impl):
                # The container builds a field-injection class without arguments
                # and without reading the signature: it cannot be checked here.
                _warn_once(("signature", impl.__module__, impl.__qualname__),
                           "%s.%s: constructor signature cannot be read (%r); it is built "
                           "without arguments and cannot be validated",
                           impl.__module__, impl.__qualname__, error)
                return []
            # The container reads the same signature and fails on it.
            return [f"{impl.__name__}: constructor signature cannot be read ({error!r})"]
        parameters = [p for p in list(signature.parameters.values())[1:] if p.kind not in {
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
        }]
        problems = []
        # Mirror the container's existing no-argument field-injection shortcut.
        if not (_field_injection_only(impl) and all(
            p.default is not inspect.Parameter.empty for p in parameters
        )):
            hints, extras, hint_error = _constructor_hints(impl.__init__)
            keyed_registrations = getattr(container, "_keyed", {})
            for parameter in parameters:
                hint = extras.get(parameter.name, parameter.annotation)
                keyed = _keyed_marker(hint)
                if keyed is None and not lifetime_only:
                    unsupported = _unsupported_keyed(impl, parameter, hint)
                    if unsupported is not None:
                        problems.append(unsupported)
                if keyed is not None:
                    child = keyed_registrations.get(keyed)
                    if child is not None:
                        problems.extend(construction_problems(child, path, singleton_owner, lifetime_only))
                    elif not lifetime_only and parameter.default is inspect.Parameter.empty:
                        problems.append(f"{impl.__name__}.{parameter.name} requires unregistered keyed {keyed!r}")
                    continue
                annotation = _unwrap_annotation(_parameter_hint(parameter, hints))
                typevar_map = getattr(reg, "typevar_map", None)
                if typevar_map and isinstance(annotation, TypeVar):
                    annotation = typevar_map.get(annotation, annotation)  # as the container does
                if hint_error is not None and isinstance(annotation, str):
                    # The container cannot resolve it either: without a default it
                    # fails, with one the default silently replaces injection.
                    if parameter.default is not inspect.Parameter.empty:
                        _warn_once(("hint", impl.__module__, impl.__qualname__, parameter.name),
                                   "%s.%s: type hint %r cannot be resolved (%s: %s); the default "
                                   "value is used instead of injection",
                                   impl.__name__, parameter.name, annotation,
                                   type(hint_error).__name__, hint_error)
                    elif not lifetime_only:
                        problems.append(f"{impl.__name__}.{parameter.name}: type hint cannot be resolved "
                                        f"({annotation!r}: {type(hint_error).__name__}: {hint_error})")
                    continue
                deferred = _deferred_wrapper(annotation)
                if deferred is not None:
                    # Provider[X] defers every resolution: never a capture. Lazy[X]
                    # keeps the first value: in a singleton that is a capture.
                    inner = registrations.get(deferred[1])
                    if inner is None and hasattr(container, "_closed_generic_registration"):
                        inner = container._closed_generic_registration(deferred[1], cache=False)
                    if not lifetime_only and inner is None:
                        problems.append(f"{impl.__name__}.{parameter.name} defers an unregistered dependency ({deferred[1]!r})")
                    if (deferred[0] is Lazy and singleton_owner is not None and inner is not None
                            and inner.scope == LifetimeScope.SCOPED):
                        problems.append(f"singleton {singleton_owner.__name__} captures scoped "
                                        f"{inner.service_type.__name__} through Lazy (it keeps the first value)")
                    elif deferred[0] is Lazy and singleton_owner is not None and inner is not None:
                        # What the Lazy builds is kept too: follow it for captures only
                        # (lifetime_only: a Lazy that breaks a cycle is no problem).
                        problems.extend(f"{problem} (through Lazy)" for problem in
                                        construction_problems(inner, path, singleton_owner, True))
                    continue
                child = registrations.get(annotation)
                if child is None and hasattr(container, "_closed_generic_registration"):
                    child = container._closed_generic_registration(annotation, cache=False)
                if annotation is not inspect.Parameter.empty and child is not None:
                    problems.extend(construction_problems(child, path, singleton_owner, lifetime_only))
                elif (not lifetime_only and annotation is inspect.Parameter.empty
                      and parameter.default is inspect.Parameter.empty):
                    problems.append(f"{impl.__name__}.{parameter.name} needs a type annotation or default")
                elif (parameter.default is inspect.Parameter.empty
                      and not lifetime_only
                      and annotation is not inspect.Parameter.empty
                      and not isinstance(annotation, str)):
                    problems.append(f"{impl.__name__}.{parameter.name} requires an unregistered dependency ({annotation!r})")
        if impl.__dict__.get("__ai_ioc_init__", False):
            for name in fields(impl):
                regs = candidates(impl, name)
                if regs:
                    selected = _pick_best_registration(regs, impl.__module__)
                    for target in targets([selected], name):
                        problems.extend(construction_problems(target, path, singleton_owner, lifetime_only))
        return problems

    issues = []
    for cls in sorted(classes, key=lambda c: (c.__module__, c.__qualname__)):
        own_candidates = set(cls.__dict__.get("__di_attrs__", ()))
        inherited = set()
        for base in cls.__mro__[1:]:
            inherited.update(name for name, value in base.__dict__.items()
                             if _DEF_ATTR_RE.match(name) and value is None
                             and name not in own_candidates)
        for name in sorted(inherited):
            # Informational only: a base constructor or explicit assignment may
            # still fill it, so it must not fail an otherwise valid startup.
            logger.warning("%s.%s.%s: inherited unannotated field is not injected by "
                           "this class's post-init injection", cls.__module__, cls.__qualname__, name)
        singleton_owner = next((reg.service_type for reg in (
                                    *registrations.values(),
                                    *getattr(container, "_keyed", {}).values(),
                                    *(item for items in earlier.values() for item in items))
                                if reg.scope == LifetimeScope.SINGLETON
                                and reg.implementation_type is cls), None)
        for name in fields(cls):
            label = f"{cls.__module__}.{cls.__qualname__}.{name}"
            regs = candidates(cls, name)
            if not regs:
                issues.append(f"{label}: no matching registration")
                continue
            tied = _tied_candidates(regs, cls.__module__)
            if len(tied) > 1:
                names = ", ".join(_registration_label(reg) for reg in tied)
                issues.append(f"{label}: ambiguous candidates: {names}")
            for reg in targets(tied, name):
                for problem in construction_problems(reg, singleton_owner=singleton_owner):
                    issues.append(f"{label}: registered but unresolvable: {problem}")
    issues.extend(untracked)
    if issues:
        raise AutowiringValidationError(issues, skipped)
    if skipped:
        logger.info("Validation does not cover modules skipped during discovery; "
                    "they are checked when they load: %s", ", ".join(skipped))
