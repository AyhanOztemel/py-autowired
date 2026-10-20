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
import dis
import logging
import weakref
from functools import wraps
from pathlib import Path
from collections import defaultdict
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Any, Tuple, get_type_hints

from .container import DependencyInjectionError, LifetimeScope, _deferred_wrapper, _keyed_marker, _parameter_hint, _slot_name, _unwrap_annotation

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
_DEF_ATTR_RE = re.compile(r'^[a-z][a-z0-9_]*_instance$')

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
_GLOBAL_LOCK = threading.RLock()
_PATCHED_CLASSES = {}


def _forget_patched_classes(key):
    with _GLOBAL_LOCK:
        _PATCHED_CLASSES.pop(key, None)


def _remember_patched_class(container, cls):
    # Identity keys avoid hashing containers or classes with custom metaclasses.
    with _GLOBAL_LOCK:
        key = id(container)
        if key not in _PATCHED_CLASSES:
            _PATCHED_CLASSES[key] = weakref.WeakValueDictionary()
            try:
                weakref.finalize(container, _forget_patched_classes, key)
            except TypeError:
                # A non-weakrefable duck-typed container remains supported.
                # Live class wrappers already hold their container by identity.
                pass
        _PATCHED_CLASSES[key][id(cls)] = cls

class AutowiringValidationError(DependencyInjectionError):
    """All statically detectable field problems from a startup check.

    ``issues`` is an immutable tuple of English diagnostic strings.
    """

    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__(
            "Autowiring validation failed:\n" + "\n".join(f"- {issue}" for issue in self.issues)
        )


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

        # Generic names
        for typ in {st, it}:
            name = typ.__name__

            # ABS_Service -> service (🎯 YENI!)
            if name.startswith('ABS_') and len(name) > 4:
                generic_key = _norm(name[4:])
                name_map[generic_key].append(reg)

            # Abs_Service -> service (🎯 YENI!)
            elif name.startswith('Abs_') and len(name) > 4:
                generic_key = _norm(name[4:])
                name_map[generic_key].append(reg)

            # Abs_Service -> service (🎯 YENI!)
            elif name.startswith('abs_') and len(name) > 4:
                generic_key = _norm(name[4:])
                name_map[generic_key].append(reg)

            # I_Service -> service
            elif name.startswith('I_') and len(name) > 2:
                generic_key = _norm(name[2:])
                name_map[generic_key].append(reg)

            # ABSService / AbsService / absService -> service (no underscore;
            # the next letter must be upper case, so "Absolute" stays a word)
            elif name[:3] in ('ABS', 'Abs', 'abs') and len(name) > 3 and name[3].isupper():
                generic_key = _norm(name[3:])
                name_map[generic_key].append(reg)

            # IService -> service (MEVCUT)
            elif name.startswith('I') and len(name) > 1 and name[1].isupper():
                generic_key = _norm(name[1:])
                name_map[generic_key].append(reg)

            # ServiceImpl -> service (MEVCUT)
            elif name.endswith('Impl'):
                generic_key = _norm(name[:-4])
                name_map[generic_key].append(reg)

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


def _resolve_via_container(container, reg):
    """ORIGINAL FUNCTION - unchanged"""
    return container.resolve(reg.service_type)

def _lookup_registration(container, name_map, normalized_name: str, context_module: str):
    """Return the registration and the container that owns its active cache.

    Patched classes live longer than a bootstrap container in tests and in
    controlled runtime rebuilds. Looking up from the new cache but resolving
    through the old closure would mix two object graphs.
    """
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

    return tuple(sorted(candidates))


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
            base = name[:-len("_instance")]
            normalized_base = _norm(base)

            if DEBUG_MODE:
                logger.debug(f"Looking up {base!r} as {normalized_base!r} (class: {cls.__name__}, module: {cls.__module__})")

            resolution_container, reg = _lookup_registration(
                container, name_map, normalized_base, cls.__module__
            )

            if reg:
                try:
                    value = _resolve_via_container(resolution_container, reg)
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

        return orig_setattr(self, name, value)

    cls.__setattr__ = __setattr__
    cls.__ai_ioc_setattr__ = True


def _patch_init(cls, container, name_map):
    """
    ORIGINAL FUNCTION - optimized internally with cache
    Post-init injection for remaining None attributes.
    """
    # 🆕 v2 FIX: getattr yerine cls.__dict__ — kalıtım bug'ı düzeltmesi
    try:
        _remember_patched_class(container, cls)
    except Exception as exc:  # opt-in validation bookkeeping must never break patching
        logger.debug(f"Validation tracking skipped for {cls!r}: {exc}")
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
        if callable(orig_init):
            orig_init(self, *args, **kwargs)
        else:
            try:
                super(cls, self).__init__()
            except Exception:
                pass

        # 🆕 v2: dir(self) yerine önceden hesaplanmış aday listesi
        for attr in di_candidates:
            try:
                val = getattr(self, attr)
            except Exception:
                logger.warning(
                    "Field read failed for %s.%s.%s; field skipped",
                    cls.__module__, cls.__qualname__, attr, exc_info=True,
                )
                continue

            if val is not None:
                continue

            base = attr[:-len("_instance")]
            normalized_base = _norm(base)

            if DEBUG_MODE:
                logger.debug(f"Post-init lookup: {base!r} as {normalized_base!r} (class: {cls.__name__}, module: {cls.__module__})")

            resolution_container, reg = _lookup_registration(
                container, name_map, normalized_base, cls.__module__
            )

            if not reg:
                if DEBUG_MODE:
                    logger.debug(f"No registration found for {normalized_base!r}")
                continue

            try:
                injected = _resolve_via_container(resolution_container, reg)
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

    # 🆕 v2: __name__ kopyalamak yerine functools.wraps + imza koruması —
    #        FastAPI/pydantic gibi introspection yapan araçlar bozulmaz.
    if callable(orig_init):
        try:
            wraps(orig_init)(__init__)
        except Exception:
            __init__.__name__ = getattr(orig_init, "__name__", "__init__")
        try:
            __init__.__signature__ = inspect.signature(orig_init)
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


def _import_module_safely(dotted_name: str, file_path: str) -> bool:
    """ORIGINAL FUNCTION - unchanged"""
    try:
        module = importlib.import_module(dotted_name)
        actual = getattr(module, "__file__", None)
        if not actual or Path(actual).resolve() != Path(file_path).resolve():
            logger.warning(
                "Module origin mismatch for %s: expected %s, imported %s",
                dotted_name, Path(file_path).resolve(), actual,
            )
            return False
        return True
    except ImportError as e:
        logger.warning(f"Import failed for {dotted_name}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Import failed for {dotted_name}: {e}")
        return False


def _normalize_excluded_dirs(exclude_dirs: Optional[Iterable[str]]) -> frozenset[str]:
    if exclude_dirs is None:
        return frozenset(_EXCLUDED_DIRS)
    if isinstance(exclude_dirs, (str, bytes)):
        raise TypeError("exclude_dirs must be an iterable of directory names")
    return frozenset(_EXCLUDED_DIRS).union(str(item) for item in exclude_dirs)


def _discover_and_load_modules(root: Path, exclude_dirs: Optional[Iterable[str]] = None) -> int:
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
            if DEBUG_MODE:
                logger.debug(f"Module path error ({p}): {e}")
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
                try:
                    _remember_patched_class(container, cls)
                except Exception:  # bookkeeping must never break patching
                    pass
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


def _remember_late_root(root: Path) -> None:
    for form in (str(root), os.path.abspath(str(root))):
        prefix = os.path.normcase(form)
        if not prefix.endswith(os.sep):
            prefix += os.sep
        if prefix not in _LATE_ROOTS:
            _LATE_ROOTS.append(prefix)
    if not any(isinstance(finder, _LateImportFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LateImportFinder())


def _drain_late_modules() -> None:
    """Patch modules that were imported while the lock was busy. Caller holds the lock."""
    cache = _NAME_MAP_CACHE
    if cache is None:
        return
    name_map = cache.get_raw_map()
    while _PENDING_LATE_MODULES:
        try:
            module = _PENDING_LATE_MODULES.pop()
        except IndexError:
            break
        try:
            _patch_module_classes(module, cache._container, name_map)
        except Exception:
            logger.warning("Could not patch late-imported module %s", module.__name__, exc_info=True)


def _patch_late_module(module) -> None:
    if getattr(_STARTUP, "active", False):
        return  # auto_inject() patches every module it loads itself
    # A bounded wait: another thread may hold the lock while it waits for this
    # very import; the module is then queued for the next lock holder.
    if not _GLOBAL_LOCK.acquire(timeout=0.5):
        _PENDING_LATE_MODULES.append(module)
        return
    try:
        _PENDING_LATE_MODULES.append(module)
        _drain_late_modules()
    finally:
        _GLOBAL_LOCK.release()


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
        if (type(loader) is importlib.machinery.SourceFileLoader
                and spec.origin and _is_late_root_path(spec.origin)):
            spec.loader = _LateImportLoader(loader.name, loader.path)
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
        _drain_late_modules()
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
    _remember_late_root(root)

    if DEBUG_MODE:
        logger.debug(f"Root directory: {root}")
        logger.debug(f"sys.path[0]: {sys.path[0]}")

    # Load modules
    if exclude_dirs is None:
        loaded_count = _discover_and_load_modules(root)
    else:
        loaded_count = _discover_and_load_modules(root, exclude_dirs)

    if loaded_count == 0 and DEBUG_MODE:
        logger.debug("No modules could be loaded")
        logger.debug("Possible causes:")
        logger.debug("1. Incorrect project root")
        logger.debug("2. No Python files found")
        logger.debug("3. All imports failed")
        logger.debug("Set root_dir explicitly:")
        logger.debug("auto_inject(container, root_dir='/absolute/path/to/project')")

    # Interfaces registered before their implementation module was imported
    # get it now, so the name map and validation see it.
    complete_implementations = getattr(container, "_complete_implementations", None)
    if complete_implementations is not None:
        complete_implementations()

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
            if not _is_rel_to(mod_path, root):
                continue
        except Exception:
            continue

        if DEBUG_MODE:
            mod_info = _get_module_depth_info(mod_name)
            logger.debug(f"Module: {mod_name} (depth: {mod_info['depth']})")

        patched += _patch_module_classes(mod, container, name_map)

    if DEBUG_MODE:
        logger.debug("Autowiring complete")
        logger.debug(f"Loaded {loaded_count} modules; patched {patched} classes")
        logger.debug("=" * 60)

    if validate:
        validate_autowiring(container)
    return patched


def validate_autowiring(container) -> None:
    """Startup checks for this container's patched classes.

    The check does not construct objects/services, call factories or field
    descriptors, or open scopes. It may evaluate type annotations, which can
    execute user code. It cannot predict arbitrary constructor/factory behavior.
    """
    with _GLOBAL_LOCK:
        classes = tuple(_PATCHED_CLASSES.get(id(container), {}).values())
    # Run outside the global lock: validation may evaluate annotations (imports)
    # and log; neither must happen while patching/refresh could be waiting.
    _validate_autowiring(container, classes)


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
        if _NAME_MAP_CACHE is not None:
            _NAME_MAP_CACHE.refresh()
            _drain_late_modules()
            if DEBUG_MODE:
                logger.debug("Name map refreshed; resolution cache cleared")
            return True
    return False


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
        _drain_late_modules()

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

def _validate_autowiring(container, classes):
    # Build a fresh view: validation neither reads nor refreshes the runtime cache.
    registrations = dict(container.registrations)  # snapshot; no nested locks
    name_map = _build_name_map_from_container(container)
    missing = object()

    def fields(cls):
        for name in cls.__dict__.get("__di_attrs__", ()):
            value = inspect.getattr_static(cls, name, missing)
            # A concrete class-level override is already satisfied. Do not invoke
            # descriptors; constructor assignments cannot be known statically.
            if value is not missing and value is not None and not inspect.isdatadescriptor(value):
                continue
            yield name

    def candidates(cls, name):
        unique = {}
        for reg in name_map.get(_norm(name[:-len("_instance")]), ()):
            unique[id(reg)] = reg  # alias/index duplicates are not ambiguity
        regs = list(unique.values())
        primaries = [reg for reg in regs if getattr(reg, "primary", False)]
        return primaries or regs  # same narrowing as runtime selection

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
            signature = inspect.signature(impl.__init__)
        except (TypeError, ValueError):
            # An opaque callable is unknown, not proof of a broken graph.
            return []
        parameters = [p for p in list(signature.parameters.values())[1:] if p.kind not in {
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
        }]
        problems = []
        # Mirror the container's existing no-argument field-injection shortcut.
        if not (getattr(impl, "__di_attrs__", ()) and all(
            p.default is not inspect.Parameter.empty for p in parameters
        )):
            try:
                hints = get_type_hints(impl.__init__)
            except (NameError, TypeError, ValueError):
                hints = {}
            try:
                extras = get_type_hints(impl.__init__, include_extras=True)
            except (NameError, TypeError, ValueError):
                extras = {}
            keyed_registrations = getattr(container, "_keyed", {})
            for parameter in parameters:
                keyed = _keyed_marker(extras.get(parameter.name, parameter.annotation))
                if keyed is not None:
                    child = keyed_registrations.get(keyed)
                    if child is not None:
                        problems.extend(construction_problems(child, path, singleton_owner, lifetime_only))
                    elif not lifetime_only and parameter.default is inspect.Parameter.empty:
                        problems.append(f"{impl.__name__}.{parameter.name} requires unregistered keyed {keyed!r}")
                    continue
                annotation = _unwrap_annotation(_parameter_hint(parameter, hints))
                deferred = _deferred_wrapper(annotation)
                if deferred is not None:
                    # Provider[X]/Lazy[X] defer resolution: never a capture,
                    # only the inner registration must exist.
                    if not lifetime_only and deferred[1] not in registrations:
                        problems.append(f"{impl.__name__}.{parameter.name} defers an unregistered dependency ({deferred[1]!r})")
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
                    problems.extend(construction_problems(selected, path, singleton_owner, lifetime_only))
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
        singleton_owner = next((reg.service_type for reg in registrations.values()
                                if reg.scope == LifetimeScope.SINGLETON
                                and reg.implementation_type is cls), None)
        for name in fields(cls):
            label = f"{cls.__module__}.{cls.__qualname__}.{name}"
            regs = candidates(cls, name)
            if not regs:
                issues.append(f"{label}: no matching registration")
                continue
            best = max(_registration_score(reg, cls.__module__) for reg in regs)
            tied = [reg for reg in regs if _registration_score(reg, cls.__module__) == best]
            if len(tied) > 1:
                names = ", ".join(
                    f"{(reg.implementation_type or reg.service_type).__module__}."
                    f"{(reg.implementation_type or reg.service_type).__qualname__}"
                    for reg in tied
                )
                issues.append(f"{label}: ambiguous candidates: {names}")
            for reg in tied:
                for problem in construction_problems(reg, singleton_owner=singleton_owner):
                    issues.append(f"{label}: registered but unresolvable: {problem}")
    if issues:
        raise AutowiringValidationError(issues)
