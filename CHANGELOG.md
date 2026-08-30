# Changelog

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
