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

## 0.5.3 and 0.5.4 additions

Both releases changed discovery, validation, the container's locking and the
bridges, so every changed line got hand-written mutants (one snippet replaced
per mutant, the related test files run against each copy). A surviving mutant
was never declared equivalent without proof: each one got a test (or the code
was simplified) and was run again.

| Release | Changed code | Mutants | Killed | Equivalent |
|---|---|---:|---:|---:|
| 0.5.3 | `_still_importing`, the circular-import branch of the module import | 15 | 15 | 0 |
| 0.5.4 | skipped-module list, load-time check on every route, `validate=True` output | 50 | 50 | 0 |
| 0.5.4 | the 15 mutants of 0.5.3 again, on the 0.5.4 code | 15 | 15 | 0 |
| 0.5.4 | "nothing hides": finalizers, tracking, WARNINGs instead of DEBUG/defaults, validation mirroring the container, first-safe-moment load, `check_skipped`, refresh/activate raising | 75 | 75 | 0 |
| 0.5.4 | independent blind review (Lazy re-entry, scoped creation gates, deferred warnings, long-wait warning, strict keyed/open generics, bridges, disposal) and the real-application second look (same ready object, read-only properties) | 51 | 51 | 0 |
| 0.5.4 | second blind review (background warning writer, Lazy and scope close through the creation gates, library-lock wait warning, late-module checks with `validate=True`, refresh ties, validation through `Lazy`/type variables, cancelled ASGI cleanup, pinned containers) | 39 | 39 | 0 |
| 0.5.4 | root fixes (positional-only parameters, one type-hint pass, `injected()` filling the built object, rebuilt signatures, deferred annotations on 3.14, optional keyed dependencies, unsupported `Keyed` forms, `validate=True` of a container without weak references) | 59 | 59 | 0 |
| 0.5.4 | third review: background writer surviving a failing handler, `_is_validating` guard, scope close rolled back on a lock cycle, `replace()` moving aliases | 10 | 10 | 0 |
| 0.5.4 | the request check (another container active -> error before anything is built; FastAPI, Flask, Django, `injected()`) | 7 | 7 | 0 |
| 0.5.4 | the name map following registration changes (`replace()` without `refresh()`), rebuild order, single read of a cached choice | 12 | 12 | 0 |
| 0.5.4 | a ready singleton returning at once (the speed fix) | 4 | 4 | 0 |
| **0.5.4 total** | | **322** | **322** | **0** |

The last run executed all 322 together on the final code: 317 with Python 3.11
and the 5 that change code used only on 3.14+ with Python 3.14, three mutants at
a time, each in its own copy of the repository, with a 300 s limit per mutant.
Before any mutant ran, the same test list ran without a mutant and had to pass
(551 passed, 3 skipped, on both interpreters); a failing baseline stops the run.
Every mutant was killed by a failing assertion; none by the time limit (the
slowest took 82 s). 10 earlier mutants are no longer run: their target code was
removed or rewritten by the root fixes (5 positional-only validation checks and
5 warnings); their successors are among the 59.

**An earlier, wrong result.** An earlier version of this section (in a local
commit, never published) reported "290 of 290 killed". That run was invalid:
one test in the list failed even without a mutant, and the tool, which stopped
at the first failure, counted every mutant as killed. The failing test showed a
real defect (`injected()` could fill a field from another application's
container), which was fixed. Since then the tool runs the baseline first, as
described above, and the whole set was run again; 7 mutants that the invalid
run had hidden survived and each got a test.

Survivors met on the way (all fixed, none equivalent): a validation path that
skipped a loaded file when the automatic load had run without an active
container (new test), a weak signature test, two duck-container conditions, a
missing "two candidates, different scores" test, a duplicated filter in the
load step (code simplified), a missing non-strict open-generic control, the 7
above, and in the last rounds: a registration made while the name map was being
rebuilt, a skipped fallback counted as a change, a cached "no registration"
answer computed again, and a transient registration carrying an instance.

### 0.5.4 final gates (after twenty review rounds)

The review rounds that followed changed the injection checks, the locking and
the thread-birth rule again, so the hand-written set grew with them. Before the
release the whole set ran once more, on the final code, each mutant in its own
copy of the repository with a 300 s limit, after the same test list had passed
without a mutant (749 passed, 33 skipped on 3.11; 779 passed, 4 skipped on
3.14t). These are the real counts; the survivors are listed, not hidden.

| Run | Mutants | Killed | Survived |
|---|---:|---:|---:|
| CPython 3.11, every mutant not tied to 3.14 | 464 | 457 | 7 |
| CPython 3.14, the mutants of code used only on 3.14+ | 5 | 5 | 0 |
| free-threaded 3.14t, the mutants of code used only there | 35 | 34 | 1 |
| free-threaded 3.14t, the locking and concurrency subset (selected by the code and the name each mutant changes) | 169 | 168 | 1 |

Survivors: on 3.11 - the name map logged twice per rebuild in debug mode, a
ready singleton built a second time when the host stops between two reads, a
second warning without its reason for a field with no registration, a store on
another object after the 256th name, a function without parameters in a base,
the bytecode check repeated on every read, and a bytecode change that only
3.13+ produces (it never ran on 3.14 in the full run: the run selected it for
3.11 only, where it is equivalent); on 3.14t - a tool id taken for a moment
although the name was already held. The subset's survivor was the first one
of the 3.11 list. None was equivalent: each got a test, and a targeted run of
each one on 3.11 and 3.14t (the bytecode one on 3.14 and 3.14t) killed all of
them. A later step (a cheaper lock count and two merged lock blocks, then a
defect the final review found in a closing scope) added mutants that left 7
more survivors: 6 got tests and were killed (one of them, a nested creation of
the same slot, had first been taken for equivalent and was not), and 1 is
equivalent. The set now has 472 active mutants.

Retired mutants: 165, each with its reason in the tool's list. 137 targeted a
line a later round rewrote and were replaced by a successor; 23 targeted code
that was removed; 5 are equivalent: a second removal of a creation gate that
is already gone, two sync/async questions that described the same set, an
identity comparison CPython never reaches, and the order of a counter only
tests read. A mutant is retired only with a reason, never deleted.

