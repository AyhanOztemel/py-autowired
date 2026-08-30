# py_autowired.py - FIXED Production-Ready Version
"""
Ultra-optimized auto-injection with full backward compatibility.
Performance: 10,000+ requests with minimal overhead.
FIX: Correctly handles nested directory structures.
"""
import re
import inspect
import sys
import importlib
import os
import threading
import dis
from functools import wraps
from pathlib import Path
from collections import defaultdict
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Any, Tuple

# ============================================================================
# CONFIGURATION
# ============================================================================
DEBUG_MODE = os.getenv("PY_AUTOWIRED_DEBUG", os.getenv("DI_DEBUG", "")).lower() in ("1", "true", "yes")
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

class AutoInjectionResolutionError(RuntimeError):
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
        'package_tree': ['.'.join(parts[:i+1]) for i in range(len(parts))]
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
    __slots__ = ('_raw_map', '_resolution_cache', '_container', '_lock')

    def __init__(self, container):
        self._container = container
        self._raw_map = None
        self._resolution_cache: Dict[Tuple[str, str], Any] = {}
        # 🆕 v2: nogil güvenliği — lazy init yarışını kapatan kilit
        self._lock = threading.Lock()

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

    def get_cached_registration(self, normalized_name: str, context_module: str) -> Optional[Any]:
        """Get best registration with aggressive caching"""
        cache_key = (normalized_name, context_module)

        # O(1) cache lookup
        if cache_key in self._resolution_cache:
            return self._resolution_cache[cache_key]

        # Cache miss - calculate and store
        raw_map = self.get_raw_map()
        regs = raw_map.get(normalized_name, [])

        if not regs:
            # 🆕 v2: setdefault — eşzamanlı yazımda tek deterministik değer
            return self._resolution_cache.setdefault(cache_key, None)

        best = _pick_best_registration(regs, context_module)
        return self._resolution_cache.setdefault(cache_key, best)


def _build_name_map_from_container(container):
    """
    ORIGINAL FUNCTION - unchanged logic
    Build name map from container registrations.
    """
    name_map = defaultdict(list)

    for binding_type, reg in container.registrations.items():
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

            # IService -> service (MEVCUT)
            elif name.startswith('I') and len(name) > 1 and name[1].isupper():
                generic_key = _norm(name[1:])
                name_map[generic_key].append(reg)

            # ServiceImpl -> service (MEVCUT)
            elif name.endswith('Impl'):
                generic_key = _norm(name[:-4])
                name_map[generic_key].append(reg)

    if DEBUG_MODE:
        print("\n📋 Name map oluşturuldu:")
        for key, regs in name_map.items():
            types = [r.service_type.__name__ for r in regs]
            print(f"  '{key}' -> {types}")

    return name_map


def _pick_best_registration(regs, ctx_module: str):
    """
    ORIGINAL FUNCTION - unchanged logic
    Pick best registration based on module proximity.
    """
    if not regs:
        return None

    if not ctx_module:
        return regs[0]

    ctx_info = _get_module_depth_info(ctx_module)
    scored_regs = []

    for reg in regs:
        it = reg.implementation_type or reg.service_type
        reg_module = getattr(it, "__module__", "")

        if not reg_module:
            scored_regs.append((reg, -1, 0))
            continue

        reg_info = _get_module_depth_info(reg_module)

        # Score calculation
        if reg_module == ctx_module:
            score = 1000
        else:
            common_depth = _calculate_module_similarity(ctx_module, reg_module)
            module_depth = reg_info['depth']
            score = common_depth * 10 + module_depth

        scored_regs.append((reg, score, reg_info['depth']))

        if DEBUG_MODE:
            print(f"    📊 {it.__name__} ({reg_module}): skor={score}, ortak_derinlik={_calculate_module_similarity(ctx_module, reg_module)}")

    # Sort by score
    scored_regs.sort(key=lambda x: (x[1], x[2]), reverse=True)

    selected = scored_regs[0][0]

    if DEBUG_MODE:
        selected_type = selected.implementation_type or selected.service_type
        print(f"    ✨ Seçilen: {selected_type.__name__} (modül: {getattr(selected_type, '__module__', 'unknown')})")

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
      1. Sınıf gövdesindeki attribute'lar   (apart_service_instance = None)
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
                print(f"  🔎 Aranan: '{base}' -> normalized: '{normalized_base}' (sınıf: {cls.__name__}, modül: {cls.__module__})")

            resolution_container, reg = _lookup_registration(
                container, name_map, normalized_base, cls.__module__
            )

            if reg:
                try:
                    value = _resolve_via_container(resolution_container, reg)
                    if DEBUG_MODE:
                        impl = (reg.implementation_type or reg.service_type).__name__
                        print(f"    ⚡ {cls.__name__}.{name} ← {impl} (eager injection)")
                except Exception as e:
                    if DEBUG_MODE:
                        print(f"    ⚠️ Injection hatası: {e}")
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
                continue

            if val is not None:
                continue

            base = attr[:-len("_instance")]
            normalized_base = _norm(base)

            if DEBUG_MODE:
                print(f"  🔎 Post-init aranan: '{base}' -> normalized: '{normalized_base}' (sınıf: {cls.__name__}, modül: {cls.__module__})")

            resolution_container, reg = _lookup_registration(
                container, name_map, normalized_base, cls.__module__
            )

            if not reg:
                if DEBUG_MODE:
                    print(f"    ❌ '{normalized_base}' için kayıt bulunamadı")
                continue

            try:
                injected = _resolve_via_container(resolution_container, reg)
                setattr(self, attr, injected)
                if DEBUG_MODE:
                    impl = (reg.implementation_type or reg.service_type).__name__
                    print(f"    💉 {cls.__name__}.{attr} ← {impl} (post-init injection)")
            except Exception as e:
                if DEBUG_MODE:
                    print(f"    ⚠️ Post-init injection hatası: {e}")
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

def _find_project_root() -> Path:
    """
    🔧 FIX: Find the actual project root directory.
    Searches for common project markers.
    """
    # Start from __main__ module's directory if available
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "__file__") and main_mod.__file__:
        current = Path(main_mod.__file__).resolve().parent
    else:
        current = Path.cwd()

    if DEBUG_MODE:
        print(f"🔍 Başlangıç dizini: {current}")

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
                    print(f"✅ Proje kökü bulundu: {parent} (marker: {marker})")
                return parent

        # Don't go beyond home directory or root
        if parent == Path.home() or parent == parent.parent:
            break

    # If no markers found, use the starting directory
    if DEBUG_MODE:
        print(f"⚠️ Marker bulunamadı, başlangıç dizini kullanılıyor: {current}")
        print(f"   Kontrol edilen dizinler: {checked_dirs[:3]}")

    return current


def _import_module_safely(dotted_name: str, file_path: str) -> bool:
    """ORIGINAL FUNCTION - unchanged"""
    try:
        importlib.import_module(dotted_name)
        return True
    except ImportError as e:
        if DEBUG_MODE:
            print(f"    ⚠️ Import hatası ({dotted_name}): {e}")
        return False
    except Exception as e:
        if DEBUG_MODE:
            print(f"    ❌ Beklenmeyen hata ({dotted_name}): {e}")
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
        print("\n📂 Modül keşfi başlatılıyor...")
        print(f"   Kök dizin: {root}")

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
        if excluded_dirs.intersection(p.parts):
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
                print(f"  📄 Yükleniyor: {dotted} ({rel})")

            if _import_module_safely(dotted, str(p)):
                loaded += 1
                if DEBUG_MODE:
                    print(f"    ✅ Başarılı")
            else:
                skipped.append((dotted, "import failed"))

        except ValueError as e:
            # File is not relative to root
            if DEBUG_MODE:
                print(f"    ⚠️ Dosya kök dizin dışında: {p}")
            skipped.append((str(p), "outside root"))
        except Exception as e:
            if DEBUG_MODE:
                print(f"    ❌ Modül yolu hatası ({p}): {e}")
            skipped.append((str(p), f"error: {e}"))

    if DEBUG_MODE:
        print(f"\n📊 Modül yükleme özeti:")
        print(f"   ✅ Başarılı: {loaded}/{total_files}")
        print(f"   ⏭️  Atlanan: {len(skipped)} dosya")
        if skipped and len(skipped) <= 5:
            for name, reason in skipped[:5]:
                print(f"      - {name}: {reason}")

    return loaded


# ============================================================================
# MAIN FUNCTION - FIXED!
# ============================================================================

def _serialized_class_patching(func):
    """Serialize global class/cache mutation without changing the public API."""
    @wraps(func)
    def locked(*args, **kwargs):
        with _GLOBAL_LOCK:
            return func(*args, **kwargs)

    return locked


@_serialized_class_patching
def auto_inject(
    container,
    root_dir: str | None = None,
    *,
    exclude_dirs: Optional[Iterable[str]] = None,
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
    Returns:
        Number of patched classes
    """
    if DEBUG_MODE:
        print("\n🚀 Auto injection başlatılıyor (Sınırsız Derinlik)...")
        print("=" * 60)

    # 🔧 FIX: Use project root finder instead of __file__
    if root_dir is None:
        root = _find_project_root()
    else:
        root = Path(root_dir).resolve()

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    if DEBUG_MODE:
        print(f"🏠 Kök dizin: {root}")
        print(f"📍 sys.path[0]: {sys.path[0]}")

    # Load modules
    if exclude_dirs is None:
        loaded_count = _discover_and_load_modules(root)
    else:
        loaded_count = _discover_and_load_modules(root, exclude_dirs)

    if loaded_count == 0 and DEBUG_MODE:
        print("\n⚠️ UYARI: Hiç modül yüklenemedi!")
        print("   Olası nedenler:")
        print("   1. Kök dizin yanlış tespit edildi")
        print("   2. Python dosyaları bulunamadı")
        print("   3. Tüm import'lar başarısız oldu")
        print(f"   Lütfen root_dir parametresini manuel olarak belirtin:")
        print(f"   auto_inject(container, root_dir='/tam/yol/proje/dizinine')")

    # Build cached name map
    global _NAME_MAP_CACHE
    _NAME_MAP_CACHE = _CachedNameMap(container)
    name_map = _NAME_MAP_CACHE.get_raw_map()

    # Patch classes
    if DEBUG_MODE:
        print("\n🔧 Sınıf patch'leme işlemi...")

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
            print(f"  📦 Modül: {mod_name} (derinlik: {mod_info['depth']})")

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
                    if DEBUG_MODE:
                        print(f"    ⏭️  {cls.__name__} atlandı (DI adayı yok)")
                    continue

            _patch_setattr(cls, container, name_map)
            _patch_init(cls, container, name_map)
            patched += 1

            if DEBUG_MODE:
                print(f"    ✅ {cls.__name__} sınıfı patch'lendi")

    if DEBUG_MODE:
        print(f"\n✨ İşlem tamamlandı!")
        print(f"📊 Sonuç: {loaded_count} modül yüklendi, {patched} sınıf patch'lendi.")
        print("=" * 60 + "\n")

    return patched


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
            if DEBUG_MODE:
                print("🔄 Name map yenilendi, çözümleme cache'i temizlendi")
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
