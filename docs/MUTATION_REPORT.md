# Mutation testing report (py_autowired)

Mutation testing changes the library's source code in small ways (for example
`==` to `!=`, `+ 1` to `+ 2`, removing a `not`) and checks whether the test
suite notices. A mutant the tests do not notice is a place where a real bug
could hide.

- Tool: cosmic-ray 8.7.0, all operators
- Scope: `src/py_autowired/autowired.py` and `src/py_autowired/container.py`
  (the injection engine and the container)
- Test command: the full test suite (`pytest -x tests`), CPython 3.11
- Run on the final 0.5.0 code; surviving mutants were then re-run one by one
  against the final tests

## Result

| | Count |
|---|---:|
| Mutants generated | 1399 |
| Killed by the tests | 1231 (88.0%) |
| Equivalent, proven automatically | 88 |
| Equivalent, proven by reasoning | 71 |
| Not killed, not proven equivalent | 9 |
| **Kill rate over non-equivalent mutants** | **99.3%** |

An *equivalent* mutant changes the code but cannot change any behavior, so no
test can ever kill it (for example changing an `lru_cache` size, or text that
is only printed in debug mode). They are excluded from the kill rate, and every
one of them is listed below with the reason.

## Equivalent mutants, proven automatically (88)

Classified by an AST script that only uses the source code:

- 36: identity vs equality (`is` / `==`) on objects without `__eq__`
  (classes, `None`, sentinels, enum members)
- 31: inside a debug-only block (`if DEBUG_MODE:`)
- 11: inside a type annotation of the library's own functions (never read at runtime)
- 6: `lru_cache` size (performance only; the cached functions are pure)
- 4: bookkeeping variables that are only read inside debug-only blocks

## Equivalent mutants, proven by reasoning (71)

- 14: prefix/underscore boundaries: name normalization removes `_`, and an
  empty key can never match an `*_instance` field
- 13: sequence invariants (a single element: `[0] == [-1]`; lengths that
  cannot occur)
- 11: debug-only control flow and values
- 7: defensive handlers whose condition cannot be reached from the public API
- 5: monotonic generation counter: only "changed or not" is observable
- 5: identity vs equality on classes, interned names and small integers
- 4: default for an attribute that every registration/handler defines
- 3: only three policy strings exist; the mutated comparison selects the same
  branch for each of them
- 2: keyword-only marker `*` changed to `/`: every caller passes the argument
  by keyword
- 2: handler inside `try/finally` that re-raises the first error anyway
- 1: enum numeric values are never used as numbers
- 1: polling interval: affects timing only, never the result
- 1: `add_note` vs `__notes__`: identical observable notes
- 1: depth is never below 1, so `<= 1` equals `== 1`
- 1: unpatched classes have no `__di_attrs__`, so the flag default cannot matter

## Not killed and not proven equivalent (9) - open work

These are counted as misses in the 99.3%. They are hard to test, not known bugs:

- 3: project-root search beyond the home/filesystem-root boundary
  (depends on the real file system above the test directory)
- 2: ordering of the library's own file vs discovered paths during discovery
- 2: module-name ordering for classes imported into another module
  (re-patching is idempotent, so the order is not visible in the tests)
- 1: the lock that serializes concurrent `auto_inject()` calls (needs a
  deterministic concurrency harness)
- 1: `__main__` without `__file__` resolves to the working directory, never a
  `.py` file

## How the tests were improved

The first full run killed far fewer mutants. Every survivor was read by hand;
each one either got a new test that pins the behavior it exposed (the
`tests/test_mutation_*.py` files and several feature tests), or was put into
one of the equivalent classes above with its reason. The library code was not
changed to make mutants disappear. Two source changes were made during this
work: one unused internal helper was removed, and one real bug found by the new
tests was fixed (the check that recognizes an explicitly written `Optional` on
Python 3.10).

## Reproducing

```bash
pip install -e ".[dev]" cosmic-ray
cosmic-ray init mutation.toml session.sqlite
cosmic-ray exec mutation.toml session.sqlite
cr-report session.sqlite
```

with `mutation.toml` containing:

```toml
[cosmic-ray]
module-path = ["src/py_autowired/autowired.py", "src/py_autowired/container.py"]
timeout = 120.0
excluded-modules = []
test-command = "python -B -m pytest -x -q -p no:cacheprovider tests"

[cosmic-ray.distributor]
name = "local"
```

## 0.5.1 additions

The code added or changed in 0.5.1 - the Flask and Django bridges, the shared
bridge module, the FastAPI shutdown and project-root changes, late-import
patching, the extra contract prefixes and the deferred implementation lookup -
was mutation tested the same way (every changed line, all operators):

| | Count |
|---|---:|
| Mutants generated | 329 |
| Killed by the tests | 285 (86.6%) |
| Equivalent, proven | 41 |
| Not killed, not proven equivalent | 3 |
| **Kill rate over non-equivalent mutants** | **99.0%** |

Equivalent: 22 inside type annotations of `fastapi.py` (never evaluated at
runtime there), 4 on ASGI scope types (`!= "http"` vs `> "http"` / identity:
only `http`, `websocket` and `lifespan` exist), 4 on the name `I_` alone (its
empty key can never match a field), 4 sequence invariants (a single candidate;
zero candidates handled before), 3 identity vs equality on classes, 2
keyword-only markers on private helpers, 1 unreachable `relative_to` fallback,
1 loop that re-checks its condition after `continue`, and 1 lock timeout value
(timing only).

Not proven (open): the `IndexError` branch when another thread empties the
late-import queue between the check and the `pop()`, and the two module-name
orderings for classes imported into another module (also open in 0.5.0).

## 0.5.2 additions

The code changed in 0.5.2 - several registrations per contract (`resolve_all`,
class-name and plural `*_instances` fields, the earlier-registration slots),
upper case in field names, and the refactored prefix helper - was mutation
tested the same way (every changed line of `container.py` and `autowired.py`,
all operators). Survivors of the first pass got new tests and were run again.

| | Count |
|---|---:|
| Mutants generated | 216 |
| Killed by the tests | 187 (86.6%) |
| Equivalent, proven | 29 |
| Not killed, not proven equivalent | 0 |
| **Kill rate over non-equivalent mutants** | **100%** |

Equivalent: 3 on the `lru_cache` of the field-key helper (size or removal:
speed only), 14 on prefix length checks (`> 4` vs `>= 4` / `!= 4` / `> 3`: a
name that starts with the prefix is never shorter, and the prefix alone gives
an empty key that no field name can produce), 4 on prefix slices one character earlier
(the extra character is the underscore, which normalization removes), 7
identity vs equality (classes; registrations, which never compare equal
because each holds its own lock; the unset marker), and 1 policy comparison
(`== "replace"` vs `>= "replace"`: the other policies, "register" and
"fallback", sort before it).
