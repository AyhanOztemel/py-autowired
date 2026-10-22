"""
Automatic field injection with cached name matching and nested module scanning.
"""
import re
import inspect
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
    _deferred_wrapper, _init_signature, _keyed_marker, _parameter_hint, _signature_of, _slot_name,
    _unsupported_keyed,
    _unwrap_annotation, _warn_once, _warn_once_background,
)

# ============================================================================
# CONFIGURATION
# ============================================================================
DEBUG_MODE = os.getenv("PY_AUTOWIRED_DEBUG", os.getenv("DI_DEBUG", "")).lower() in ("1", "true", "yes")
logger = logging.getLogger("py_autowired")
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

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking or timeout >= 0:
            got = self._lock.acquire(blocking, timeout)
        else:
            got = self._lock.acquire(blocking=False) or self._wait()
        if got:
            self._owner = threading.get_ident()
            self._depth += 1
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
        if not self._depth:
            self._owner = None
        self._lock.release()

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
    __slots__ = ('_raw_map', '_resolution_cache', '_container', '_lock', '_generation')

    def __init__(self, container):
        self._container = container
        self._raw_map = None
        self._resolution_cache: Dict[Tuple[str, str], Any] = {}
        # 🆕 v2: nogil güvenliği — lazy init yarışını kapatan kilit
        self._lock = threading.Lock()
        # Bumped by refresh(); lets lock-free lookups detect a concurrent refresh.
        self._generation = 0

    def get_raw_map(self) -> dict:
        """Get raw name map - built once, reused (🆕 v2: thread-safe)"""
        # Double-checked locking: sıcak yolda kilitsiz okuma
        if self._raw_map is None:
            with self._lock:
                if self._raw_map is None:
                    self._raw_map = _build_name_map_from_container(self._container)
        return self._raw_map

    def refresh(self):
        """🆕 v2: Geç kayıtlar için haritayı ve cache'i yeniden kur."""
        with self._lock:
            self._raw_map = _build_name_map_from_container(self._container)
            self._resolution_cache.clear()
            self._generation += 1

    def get_cached_registration(self, normalized_name: str, context_module: str) -> Optional[Any]:
        """Get best registration with aggressive caching"""
        cache_key = (normalized_name, context_module)

        # O(1) cache lookup (lock-free hot path, as in 0.4.0)
        if cache_key in self._resolution_cache:
            return self._resolution_cache[cache_key]

        # Cache miss - calculate and store (still lock-free). The generation is
        # read BEFORE the map, so a refresh() during the computation is detected.
        generation = self._generation
        raw_map = self.get_raw_map()
        regs = raw_map.get(normalized_name, [])
        best = _pick_best_registration(regs, context_module) if regs else None
        # 🆕 v2: setdefault — eşzamanlı yazımda tek deterministik değer
        stored = self._resolution_cache.setdefault(cache_key, best)
        if self._generation != generation:
            # A refresh() ran meanwhile: never let a pre-refresh choice persist.
            if self._resolution_cache.get(cache_key) is best:
                self._resolution_cache.pop(cache_key, None)
            return best
        return stored

def _build_name_map_from_container(container):
    """
    ORIGINAL FUNCTION - unchanged logic
    Build name map from container registrations.
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

    if DEBUG_MODE:
        logger.debug("Name map built:")
        for key, regs in name_map.items():
            types = [r.service_type.__name__ for r in regs]
            logger.debug(f"  {key!r} -> {types}")

    return name_map


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
    """
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

    # 2) Annotation'lar (kalıtım dahil — __init__ super zinciriyle set edebilir)
    for klass in cls.__mro__:
        for name in getattr(klass, "__annotations__", {}):
            if _DEF_ATTR_RE.match(name):
                candidates.add(name)

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

    # A method, property or other descriptor with a field-like name (for example
    # a ``has_instance`` property) is not a field: injection must not touch it.
    return tuple(sorted(name for name in candidates if not _is_accessor(cls, name)))


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
                _warn_once(("unregistered", cls.__module__, cls.__qualname__, name),
                           "%s.%s.%s: no matching registration; the field stays None",
                           cls.__module__, cls.__qualname__, name)

        return orig_setattr(self, name, value)

    cls.__setattr__ = __setattr__
    cls.__ai_ioc_setattr__ = True


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
            _warn_once(("unregistered", cls.__module__, cls.__qualname__, attr),
                       "%s.%s.%s: no matching registration; the field stays None",
                       cls.__module__, cls.__qualname__, attr)
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
        di_candidates = _find_di_candidates(cls)
        cls.__di_attrs__ = di_candidates

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
                candidates = _find_di_candidates(cls)
                try:
                    cls.__di_attrs__ = candidates
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
    return container in _VALIDATING or id(container) in _VALIDATING_PINNED


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


class _LateImportFinder(importlib.abc.MetaPathFinder):
    """Asks the other finders and marks project source modules for patching."""

    def find_spec(self, fullname, path, target=None):
        if not _LATE_ROOTS:
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


def _inject_unpatched(instance, container) -> None:
    """injected() built an object of a class discovery never patched (outside the
    project root, or created after startup): fill the None fields of that ONE
    object. The class is not changed - modules outside the project are never
    touched - so a field used inside __init__ right after ``= None`` is not
    filled yet there."""
    cls = type(instance)
    if cls.__dict__.get("__ai_ioc_init__", False):
        return  # its patched __init__ has filled it
    try:
        candidates = _INJECTED_FIELDS[cls]
    except KeyError:
        candidates = _INJECTED_FIELDS[cls] = _find_di_candidates(cls)
    name_map = {} if _NAME_MAP_CACHE is not None else _build_name_map_from_container(container)
    _inject_fields(instance, cls, container, name_map, candidates)


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
            if getattr(impl, "__di_attrs__", ()):
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
        if not (getattr(impl, "__di_attrs__", ()) and all(
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
