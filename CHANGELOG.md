# Changelog

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
