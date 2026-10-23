"""Framework-independent dependency injection container."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache, wraps
import inspect
import logging
import queue
import re
import sys
import threading
import time
import types
from types import TracebackType
from typing import Annotated, Any, Callable, Dict, Generic, Optional, Type, TypeVar, Union, get_args, get_origin, get_type_hints


T = TypeVar("T")
_UNSET = object()
class _HeldLogger:
    """The library's logger. While this thread holds a library lock (_HELD), a record is kept and
    written when the last one is released - a log handler may take locks of its own. Every log
    call of the library goes through it; anything else (handlers, level) is the real logger's."""

    __slots__ = ("_logger",)

    def __init__(self, real: logging.Logger) -> None:
        self._logger = real

    def _log(self, level: int, message: str, args: tuple, kwargs: dict) -> None:
        # No isEnabledFor() here: on a cache miss it takes logging's own lock, which dictConfig holds
        # while it runs the user's factories - under a library lock that can be a cycle. log() checks
        # the level itself, outside the library locks (now, or when a kept record is written).
        if _HELD.count:
            if kwargs.get("exc_info") is True:  # the exception of now, not of the release
                kwargs["exc_info"] = sys.exc_info()
            _HELD.__dict__.setdefault("pending", []).append((level, message, args, kwargs))
        else:
            self._logger.log(level, message, *args, stacklevel=3, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, args, kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, message, args, kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, message, args, kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, args, kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)


logger = _HeldLogger(logging.getLogger("py_autowired"))
_WARNED: set = set()  # keys of the once-only warnings already written
# A leaf lock: only the set's check and add run under it (on plain keys only: no user code), so
# the check and the add are one step on the free-threaded build too. The log line is written
# outside it.
_WARNED_LOCK = threading.Lock()
# The library locks this thread holds now. A log handler may take its own locks (or wait for
# another thread): while this thread holds ANY library lock, _warn_once keeps its WARNING and the
# release of the last one writes them all (0.5.4 round 19: the measure is the locks held, not the
# resolution depth - a resolution that takes no lock pays nothing).
class _HeldLocks(threading.local):
    count = 0  # class defaults: what a thread that holds nothing reads
    pending = None


_HELD = _HeldLocks()


def _hold() -> None:
    _HELD.count += 1


def _unhold() -> None:
    """After a library lock was released: the last one writes what was kept meanwhile. Never below
    zero (a negative count would keep every later WARNING forever)."""
    held = _HELD
    if held.count > 0:
        held.count -= 1
    if not held.count and held.pending:
        _flush_warnings()


class _LibraryLock:
    """A library lock (re-entrant or not) that counts itself as held by this thread while taken -
    see _HeldLocks. Used with ``with`` or acquire()/release()."""

    __slots__ = ("_lock",)

    def __init__(self, reentrant: bool = False) -> None:
        self._lock = threading.RLock() if reentrant else threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._lock.acquire(blocking, timeout)
        if got:
            _HELD.count += 1
        return got

    def release(self) -> None:
        self._lock.release()
        held = _HELD
        count = held.count
        if count > 0:
            count -= 1
            held.count = count
        if not count and held.pending:
            _flush_warnings()

    def _is_owned(self) -> bool:
        return self._lock._is_owned()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        self._lock.acquire()
        _HELD.count += 1
        return True

    def __exit__(self, *exc_info: Any) -> None:
        self._lock.release()
        held = _HELD
        count = held.count
        if count > 0:
            count -= 1
            held.count = count
        if not count and held.pending:
            _flush_warnings()


def _first_time(key) -> bool:
    """True once per key. A key with a part that is not exactly a str or None (its type checked
    with `is`: no user code) is never noted - its WARNING is written every time, and nothing of
    the user's is kept or hashed under the lock."""
    for part in key:
        if type(part) is not str and part is not None:
            return True
    with _WARNED_LOCK:
        if key in _WARNED:
            return False
        _WARNED.add(key)
        return True


def _warn_once(key, message: str, *args: Any) -> None:
    """A WARNING that repeats nothing: one line per problem, not per call."""
    if not _first_time(key):
        return
    logger.warning(message, *args)  # kept while a library lock is held (_HeldLogger)


class _BackgroundLog:
    """One lazily started daemon thread that writes the warnings of threads that
    must not run log handlers themselves (they wait, or hold library locks).
    put() never waits for a handler; when the queue is full the oldest entry is
    dropped and the number dropped is written with the next one."""

    def __init__(self, size: int = 256) -> None:
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=size)
        self._count = threading.Lock()  # guards _dropped only; never held while logging
        self._dropped = 0
        self._start = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def put(self, message: str, args: tuple, exc_info: Any = None) -> None:
        while True:
            try:
                self._queue.put_nowait((message, args, exc_info))
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                with self._count:
                    self._dropped += 1
        if self._thread is None:
            with self._start:
                if self._thread is None:
                    self._thread = threading.Thread(
                        target=self._run, name="py_autowired-log", daemon=True)
                    self._thread.start()

    def _run(self) -> None:
        while True:
            message, args, exc_info = self._queue.get()
            with self._count:
                dropped, self._dropped = self._dropped, 0
            lines = [("%d py_autowired warnings were dropped (the warning queue was full)", (dropped,), None)]
            for text, values, info in (lines if dropped else []) + [(message, args, exc_info)]:
                try:
                    logger.warning(text, *values, exc_info=info)
                except Exception:  # a failing log handler must never stop the writer
                    try:
                        sys.stderr.write("py_autowired: " + (text % values) + "\n")
                    except Exception:
                        pass  # stderr is gone too: nothing is left to write to


_BACKGROUND_LOG = _BackgroundLog()


def _warn_once_background(key, message: str, *args: Any) -> None:
    """_warn_once for a thread that must not run log handlers itself."""
    if not _first_time(key):
        return
    _BACKGROUND_LOG.put(message, args)


def _flush_warnings() -> None:
    pending = _HELD.pending
    while pending:
        level, message, args, kwargs = pending.pop(0)
        logger._logger.log(level, message, *args, **kwargs)


# The scope state this context is closing: its finalizers (and what they start with a copy of
# their context) may still use the objects not finalized yet. The name starts with
# "py_autowired": a 3.14t thread does not inherit it at birth (_thread_birth).
_CLOSING: "contextvars.ContextVar[Optional[_ScopeState]]" = contextvars.ContextVar(
    "py_autowired_closing", default=None)

# The singleton being created in this context (innermost), for N6.
_SINGLETON_IN_CREATION: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "py_autowired_singleton_in_creation", default=None)


class LifetimeScope(Enum):
    """Supported service lifetimes."""

    SINGLETON = 1
    SCOPED = 2
    TRANSIENT = 3


class DependencyInjectionError(RuntimeError):
    """Base error raised by py-autowired."""


class ServiceNotRegisteredError(KeyError, DependencyInjectionError):
    """Raised when a requested service has no registration."""


class DuplicateServiceRegistrationError(ValueError, DependencyInjectionError):
    """Raised when strict registration would replace a binding."""


class MissingServiceReplacementError(KeyError, DependencyInjectionError):
    """Raised when a replacement has no binding to replace."""


class ScopeNotActiveError(DependencyInjectionError):
    """Raised when a scoped service is resolved without an active scope."""


class CircularDependencyError(DependencyInjectionError):
    """Raised when dependency resolution contains a cycle."""


class ResolutionError(DependencyInjectionError):
    """Raised when a service cannot be constructed."""


class AmbiguousImplementationError(DependencyInjectionError):
    """Raised when multiple loaded implementations match an abstraction."""


class AsyncDisposalRequiredError(DependencyInjectionError):
    """Raised when an async resource is closed through a synchronous scope."""


class ContainerClosedError(DependencyInjectionError):
    """Raised when resolving from a container after close()/aclose()."""


class AsyncFactoryError(DependencyInjectionError):
    """A synchronous resolution received a bare coroutine instead of a service."""


def _raise_disposal_errors(errors: list) -> None:
    """Raise the first disposal error as before; never lose the others."""
    first = errors[0]
    try:
        for error in errors[1:]:
            try:
                text = str(error)
            except Exception:  # a broken __str__ must not mask the first error
                text = "<unprintable>"
            note = f"Additional disposal error: {type(error).__name__}: {text}"
            reported = False
            try:  # the note first: a failing log handler must not lose it
                if hasattr(first, "add_note"):
                    first.add_note(note)
                else:  # Python 3.10 has no add_note(); same __notes__ contract.
                    first.__notes__ = [*getattr(first, "__notes__", ()), note]
                reported = True
            except Exception:
                pass
            try:
                logger.error(note, exc_info=(type(error), error, error.__traceback__))
                reported = True
            except Exception:
                pass
            if not reported:  # both channels failed: the last resort
                try:
                    sys.stderr.write(note + "\n")
                except Exception:
                    pass
    finally:
        raise first


@dataclass
class ServiceRegistration:
    service_type: Type[Any]
    implementation_type: Optional[Type[Any]] = None
    factory: Optional[Callable[["Container"], Any]] = None
    scope: LifetimeScope = LifetimeScope.TRANSIENT
    instance: Any = _UNSET
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    primary: bool = False  # opt-in @Primary for name-matched field candidates
    key: Any = _UNSET  # opt-in keyed service; unkeyed registrations keep _UNSET
    typevar_map: Any = None  # closed open-generic: {TypeVar: concrete type}
    # Set when a later registration of the same type takes over: the earlier one
    # stays usable (by class name, resolve_all) under its own scope slot.
    variant: Any = _UNSET
    supplied: bool = False  # register_instance: the object belongs to the caller

    @property
    def slot(self) -> Any:
        """Scope-cache / cycle-stack identity: the type, (type, key) if keyed,
        (type, variant) for an earlier registration kept beside the current one."""
        if self.key is not _UNSET:
            return (self.service_type, self.key)
        if self.variant is not _UNSET:
            return (self.service_type, self.variant)
        return self.service_type

    @property
    def has_instance(self) -> bool:
        return self.instance is not _UNSET


def factory_dependencies(*service_types: Type[Any]):
    """Declare a factory's hidden dependencies for graph validation."""

    def decorate(factory: Callable[["Container"], Any]):
        setattr(factory, "__di_dependencies__", tuple(service_types))
        return factory

    return decorate


class _Variant:
    """Unique slot marker of an earlier registration; prints as its class name."""

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return self.label


def _same_target(first: ServiceRegistration, second: ServiceRegistration) -> bool:
    """Both create the same class the same way (re-registration, not a second one)."""
    return (
        first.implementation_type is not None
        and first.implementation_type is second.implementation_type
        and first.factory is None
        and second.factory is None
        and not first.supplied
        and not second.supplied
    )


def _concrete_subclasses(service_type: Type[Any]) -> list[Type[Any]]:
    found: list[Type[Any]] = []
    seen: set[Type[Any]] = set()

    def visit(candidate: Type[Any]) -> None:
        for child in candidate.__subclasses__():
            if child in seen:
                continue
            seen.add(child)
            if not inspect.isabstract(child):
                found.append(child)
            visit(child)

    visit(service_type)
    return found


# Runs on the hot creation path, so the signature inspection is memoised. The
# bounded cache keeps dynamically built classes from accumulating. Re-patching a
# class cannot stale the answer: auto_inject copies the original __signature__
# onto the wrapper it installs.
def _read_signature(function: Any) -> inspect.Signature:
    """``inspect.signature``; on 3.14+ an annotation that cannot be evaluated yet
    no longer breaks it: the annotations are then read as strings (what ``from
    __future__ import annotations`` gives) and the hint problem is reported where
    type hints are read."""
    try:
        return inspect.signature(function)
    except NameError:
        if sys.version_info < (3, 14):
            raise
        import annotationlib
        return inspect.signature(function, annotation_format=annotationlib.Format.STRING)


def _signature_from_code(function: Any) -> Optional[inspect.Signature]:
    """The signature of a plain function read from its own code (the parameters
    Python really checks), ignoring a ``__signature__`` that cannot be read."""
    try:
        target = inspect.unwrap(function)
    except ValueError:
        return None
    if not isinstance(target, types.FunctionType):
        return None
    clone = types.FunctionType(target.__code__, target.__globals__, target.__name__,
                               target.__defaults__, target.__closure__)
    clone.__kwdefaults__ = target.__kwdefaults__
    annotate = getattr(target, "__annotate__", None)
    if annotate is not None:  # 3.14+: keep the annotations deferred
        clone.__annotate__ = annotate
    else:
        clone.__annotations__ = dict(getattr(target, "__annotations__", None) or {})
    try:
        return _read_signature(clone)
    except (TypeError, ValueError):
        return None


def _signature_of(function: Any, owner: Any) -> inspect.Signature:
    """The signature, also where ``inspect.signature`` fails although the
    parameters are known (see ``_read_signature``). A ``__signature__`` that
    cannot be read is rebuilt from the function's code. Any other error - and a
    callable without code - raises as before.
    """
    try:
        return _read_signature(function)
    except (TypeError, ValueError) as error:
        rebuilt = _signature_from_code(function)
        if rebuilt is None:
            raise
        module = getattr(owner, "__module__", "?")
        name = getattr(owner, "__qualname__", repr(owner))
        _warn_once(("rebuilt-signature", module, name),
                   "%s.%s: its __signature__ cannot be read (%r); the signature is rebuilt "
                   "from its code", module, name, error)
        return rebuilt


def _init_signature(implementation: Type[Any]) -> inspect.Signature:
    return _signature_of(implementation.__init__, implementation)


def _has_metadata(hint: Any) -> bool:
    if get_origin(hint) is Annotated:
        return True
    for argument in get_args(hint):
        arguments = argument if isinstance(argument, (list, tuple)) else (argument,)
        if any(_has_metadata(item) for item in arguments):
            return True
    return False


def _constructor_hints(function: Any) -> tuple[Dict[str, Any], Dict[str, Any], Optional[Exception]]:
    """(plain hints, hints with metadata, error), read in one pass.

    The plain view is the same dict unless a hint carries metadata (Keyed); only
    then is it read again. One failure fails both: Keyed metadata is never lost
    while the plain hints resolve, so a keyed parameter can never fall back to
    the unkeyed registration.
    """
    try:
        extras = get_type_hints(function, include_extras=True)
    except (NameError, TypeError, ValueError) as error:
        return {}, {}, error
    if not any(_has_metadata(hint) for hint in extras.values()):
        return extras, extras, None
    try:
        return get_type_hints(function), extras, None
    except (NameError, TypeError, ValueError) as error:
        return {}, extras, error


def _field_injection_only(implementation: Type[Any]) -> bool:
    """True when the class has *_instance fields of its OWN: auto_inject fills them after
    construction and its __init__ is not resolved. A field it only inherits from a base outside
    the project does not count (0.5.4): its constructor dependencies are resolved as before."""
    own = getattr(implementation, "__di_own_attrs__", None)
    if own is None:
        own = getattr(implementation, "__di_attrs__", ())  # set before 0.5.4 or by hand
    return bool(own)


@lru_cache(maxsize=1024)
def _accepts_no_arguments(implementation: Type[Any]) -> bool:
    try:
        signature = _init_signature(implementation)
    except (TypeError, ValueError):
        return True
    for parameter in list(signature.parameters.values())[1:]:
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if parameter.default is inspect.Parameter.empty:
            return False
    return True


class Provider(Generic[T]):
    """Resolve ``service_type`` on every call (opt-in deferred resolution).

    The registered lifetime is respected at call time: a scoped service comes
    from the scope active when the provider is called. A singleton may hold a
    ``Provider`` of a scoped service without capturing it.
    """

    __slots__ = ("_container", "_service_type")

    def __init__(self, container: "Container", service_type: Type[T]) -> None:
        self._container = container
        self._service_type = service_type

    def __call__(self) -> T:
        return self._container.resolve(self._service_type)

    def __repr__(self) -> str:
        return f"Provider[{getattr(self._service_type, '__name__', self._service_type)}]"


class Lazy(Generic[T]):
    """Resolve ``service_type`` once, on first ``.value`` access."""

    __slots__ = ("_container", "_service_type", "_value", "_gate", "_resolving")

    def __init__(self, container: "Container", service_type: Type[T]) -> None:
        self._container = container
        self._service_type = service_type
        self._value: Any = _UNSET
        # Taken like a singleton's creation lock: a cycle across threads raises,
        # a hidden wait is reported (creation_wait_warning).
        self._gate = _CreationGate(self)
        self._resolving: Optional[int] = None  # thread that is resolving the value now

    @property
    def value(self) -> T:
        if self._value is _UNSET:
            if self._resolving == threading.get_ident():
                # Read again by its own resolution: waiting on the lock would hang
                # and resolving again would build a second object. Neither: say so.
                raise CircularDependencyError(
                    f"{self!r}.value was read again while it is being resolved"
                )
            container = self._container
            container._acquire_creation(self._gate)
            try:
                if self._value is _UNSET:
                    self._resolving = threading.get_ident()
                    try:
                        self._value = container.resolve(self._service_type)
                    finally:
                        self._resolving = None
            finally:
                container._release_creation(self._gate)
        return self._value

    @property
    def is_value_created(self) -> bool:
        return self._value is not _UNSET

    def __repr__(self) -> str:
        return f"Lazy[{getattr(self._service_type, '__name__', self._service_type)}]"


class Keyed:
    """Annotation marker: ``Annotated[T, Keyed(key)]`` selects a keyed registration."""

    __slots__ = ("key",)

    def __init__(self, key: Any) -> None:
        self.key = key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Keyed) and other.key == self.key

    def __hash__(self) -> int:
        return hash((Keyed, self.key))

    def __repr__(self) -> str:
        return f"Keyed({self.key!r})"


_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


def _without_none(annotation: Any) -> Any:
    """``T`` for ``Optional[T]``, ``Union[T, None]`` and ``T | None``; else unchanged."""
    if get_origin(annotation) in _UNION_ORIGINS:
        args = get_args(annotation)
        if len(args) == 2 and type(None) in args:
            return args[0] if args[1] is type(None) else args[1]
    return annotation


def _keyed_marker(annotation: Any) -> Optional[tuple[Any, Any]]:
    """Return (type, key) for ``Annotated[T, Keyed(key)]``, also as an optional
    dependency - inside ``Optional``/``| None``, or ``Annotated[Optional[T], ...]`` -
    else None."""
    annotation = _without_none(annotation)
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, Keyed):
                return _without_none(args[0]), meta.key
    return None


def _stray_keyed(annotation: Any) -> Optional[Keyed]:
    """A Keyed marker anywhere in a hint (for a form ``_keyed_marker`` does not take)."""
    if get_origin(annotation) is Annotated:
        for meta in get_args(annotation)[1:]:
            if isinstance(meta, Keyed):
                return meta
    for argument in get_args(annotation):
        for item in argument if isinstance(argument, (list, tuple)) else (argument,):
            found = _stray_keyed(item)
            if found is not None:
                return found
    return None


def _unsupported_keyed(owner: Any, parameter: inspect.Parameter, hint: Any) -> Optional[str]:
    """The line for a Keyed marker that would be ignored, else None."""
    marker = _stray_keyed(hint)
    if marker is None:
        return None
    return (f"{owner.__name__}.{parameter.name}: Keyed({marker.key!r}) is in a form that is not "
            f"supported ({hint!r}); it is ignored - write Annotated[T, Keyed(key)], "
            "optionally inside Optional[...]")


# Cross-thread cycle detection for FIRST singleton creation only. The small
# bookkeeping lock guards two dicts and is never held while user code runs.
_WAIT_LOCK = threading.Lock()
_LOCK_OWNERS: Dict[int, tuple] = {}   # id(registration) -> (thread ident, depth)
_WAITING: Dict[int, Any] = {}         # thread ident -> registration it waits for


def _thread_wait_cycle(me: int) -> Optional[list]:
    """Return the registrations of a wait-for cycle through thread ``me``."""
    with _WAIT_LOCK:
        chain, seen, thread = [], set(), me
        while True:
            registration = _WAITING.get(thread)
            if registration is None:
                return None
            chain.append(registration)
            owner = _LOCK_OWNERS.get(id(registration))
            if owner is None:
                return None
            thread = owner[0]
            if thread == me:
                return chain
            if thread in seen:
                return None
            seen.add(thread)


def _slot_name(slot: Any) -> str:
    if isinstance(slot, tuple) and len(slot) == 2:
        return f"{getattr(slot[0], '__name__', slot[0])}[{slot[1]!r}]"
    return getattr(slot, "__name__", repr(slot))


def _deferred_wrapper(annotation: Any) -> Optional[tuple[Any, Any]]:
    """Return (wrapper class, inner type) for Provider[X]/Lazy[X], else None."""
    origin = get_origin(annotation)
    if origin in (Provider, Lazy):
        args = get_args(annotation)
        if args:
            return origin, args[0]
    return None


def _parameter_hint(parameter: inspect.Parameter, hints: Dict[str, Any]) -> Any:
    """The parameter's type hint, identical on every supported Python version.

    Python 3.10's ``get_type_hints`` turns ``x: T = None`` into ``Optional[T]``;
    3.11+ keeps ``T``. Drop only that implicit ``Optional`` so injection does not
    depend on the interpreter. An explicitly written Optional/Union is kept.
    """
    annotation = hints.get(parameter.name, parameter.annotation)
    if parameter.default is not None or get_origin(annotation) is not Union:
        return annotation
    args = get_args(annotation)
    if len(args) != 2 or type(None) not in args:
        return annotation
    raw = parameter.annotation
    if isinstance(raw, str):
        explicit = re.search(r"\b(Optional|Union|None)\b|\|", raw) is not None
    else:
        explicit = get_origin(raw) is Union or type(raw).__name__ == "UnionType"
    if explicit:
        return annotation
    return args[0] if args[1] is type(None) else args[1]


def _unwrap_annotation(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def _first_callable(instance: Any, *names: str) -> Optional[Callable[[], Any]]:
    """The first finalizer that can be called: a flag named ``dispose`` must not
    hide a real ``close()``."""
    for name in names:
        finalizer = getattr(instance, name, None)
        if callable(finalizer):
            return finalizer
    return None


class _CreationGate:
    """One scoped slot being created in one scope (the wait-for graph key)."""

    __slots__ = ("slot", "lock", "__weakref__")

    def __init__(self, slot: Any) -> None:
        self.slot = slot
        self.lock = threading.RLock()  # counted as held by _acquire_creation, like a singleton's


class _ScopeState:
    def __init__(self, container: "Container") -> None:
        self.container = container
        self.instances: Dict[Type[Any], Any] = {}
        self.creation_order: list[Type[Any]] = []
        # Guards the dictionaries only; user code never runs under it.
        self.lock = _LibraryLock(reentrant=True)
        self.gates: Dict[Any, _CreationGate] = {}  # slot -> gate of its creation in progress
        self.closed = False
        # The thread that entered it (Scope.__enter__; the 3.14t hard rule): the Thread object itself,
        # held here, so no later thread can ever be "the same" (a numeric id can be reused).
        self.thread = threading.current_thread()
        # While closing: the objects whose finalizer has not run yet. "instances" is emptied at
        # the start of the close, so the lock-free path below always misses then.
        self._closing: Dict[Type[Any], Any] = {}

    def get_or_create(self, registration: ServiceRegistration, container: "Container") -> Any:
        slot = registration.slot  # == service_type for every unkeyed registration
        instance = self.instances.get(slot, _UNSET)
        if instance is not _UNSET and not self.closed:
            # Lock-free only while the scope is open: once closed, the close may still be waiting
            # for a creation and its objects are about to be disposed - the locked path decides.
            return instance
        with self.lock:
            if self.closed:
                return self._from_closing(slot)
            instance = self.instances.get(slot, _UNSET)
            if instance is not _UNSET:
                return instance
            gate = self.gates.get(slot)
            if gate is None:
                gate = self.gates[slot] = _CreationGate(slot)
        # One gate per slot of THIS scope, taken like a singleton's creation lock:
        # other slots (and other scopes) are not blocked, and a lock cycle across
        # threads raises CircularDependencyError instead of hanging.
        container._acquire_creation(gate)
        gate_removed = False
        try:
            with self.lock:
                if self.closed:
                    return self._from_closing(slot)
                instance = self.instances.get(slot, _UNSET)
            if instance is _UNSET:
                instance = container._create_instance(registration)
                with _WAIT_LOCK:
                    owner = _LOCK_OWNERS.get(id(gate))
                    outermost = owner is None or owner[1] <= 1
                with self.lock:
                    self.instances[slot] = instance
                    self.creation_order.append(slot)
                    if outermost and self.gates.get(slot) is gate:
                        del self.gates[slot]
                gate_removed = True
            return instance
        finally:
            if not gate_removed:
                with _WAIT_LOCK:
                    owner = _LOCK_OWNERS.get(id(gate))
                    outermost = owner is None or owner[1] <= 1
                if outermost:
                    with self.lock:
                        if self.gates.get(slot) is gate:
                            del self.gates[slot]
            container._release_creation(gate)

    def _from_closing(self, slot: Any) -> Any:
        """The scope is closed or closing (the caller holds the lock). Only the code closing it
        gets an object, and only one whose finalizer has not run yet - never a disposed one."""
        if _CLOSING.get() is self:
            instance = self._closing.get(slot, _UNSET)
            if instance is not _UNSET:
                return instance
        raise ScopeNotActiveError("The active dependency scope is already closed")

    def _take_for_finalizing(self, slot: Any, finalizer_of: Callable[[Any], Any]) -> tuple:
        """(object, finalizer) of ``slot`` - the finalizer read once - with the object taken out of
        the scope BEFORE its finalizer runs: nobody gets it once it is being disposed. An object
        without a finalizer is never disposed and stays available to the closing code until the
        close ends: (_UNSET, None)."""
        with self.lock:
            instance = self._closing.get(slot, _UNSET)
        if instance is _UNSET:
            return _UNSET, None
        finalizer = finalizer_of(instance)  # outside the lock: may run the object's own code
        if finalizer is None:
            return _UNSET, None
        with self.lock:
            if self._closing.get(slot, _UNSET) is instance:
                del self._closing[slot]
        return instance, finalizer

    @staticmethod
    def _sync_finalizer(instance: Any) -> Optional[Callable[[], Any]]:
        """What a sync close calls, read once: dispose/close; for an object that only has
        aclose, a call that reports it; None: nothing to call (never disposed)."""
        finalizer = _first_callable(instance, "dispose", "close")
        if finalizer is None and callable(getattr(instance, "aclose", None)):
            def requires_async() -> None:
                raise AsyncDisposalRequiredError(
                    f"{type(instance).__name__} requires 'async with container.create_scope()'"
                )
            return requires_async
        return finalizer

    @staticmethod
    def _async_finalizer(instance: Any) -> Optional[Callable[[], Any]]:
        return _first_callable(instance, "aclose", "dispose", "close")

    @staticmethod
    def _sync_finalize(instance: Any, finalizer: Any = _UNSET) -> None:
        if finalizer is _UNSET:
            finalizer = _ScopeState._sync_finalizer(instance)
        if finalizer is None:
            return
        result = finalizer()
        if inspect.isawaitable(result):
            close_coroutine = getattr(result, "close", None)
            if callable(close_coroutine):
                close_coroutine()
            raise AsyncDisposalRequiredError(
                f"{type(instance).__name__} returned an awaitable finalizer; use an async scope"
            )

    @staticmethod
    async def _async_finalize(instance: Any, finalizer: Any = _UNSET) -> None:
        if finalizer is _UNSET:
            finalizer = _ScopeState._async_finalizer(instance)
        if finalizer is None:
            return
        result = finalizer()
        if inspect.isawaitable(result):
            await result

    def _begin_close(self) -> Optional[list]:
        # Flag under the scope lock; later creations see closed. An in-flight
        # creation finishes first and is included: wait for each open gate.
        # No finalizer runs under the lock.
        me = threading.get_ident()
        with self.lock:
            if self.closed:
                return None
            gates = list(self.gates.values())
            for gate in gates:
                owner = _LOCK_OWNERS.get(id(gate))
                if owner is not None and owner[0] == me:
                    # Waiting for our own creation would hang; closing under it
                    # would store the new object in a closed scope, never closed.
                    raise DependencyInjectionError(
                        f"A scope cannot be closed while this thread is creating "
                        f"{_slot_name(gate.slot)} in it"
                    )
            self.closed = True
            if not gates:  # nothing in flight: swap at once (the same order as below)
                self._closing, self.instances = self.instances, {}
                return list(self.creation_order)
        for gate in gates:
            # Through the wait-for graph: a lock cycle raises, a hidden wait is reported.
            try:
                self.container._acquire_creation(gate)
            except BaseException:
                # This thread holds a lock that creation waits for: closing now would run
                # finalizers under it. Nothing is closed - the scope stays open and whole
                # (the creation finishes into it); close it again without that lock.
                with self.lock:
                    self.closed = False
                raise
            self.container._release_creation(gate)
        with self.lock:
            # Every creation has finished: swap the objects out of the lock-free path.
            self._closing, self.instances = self.instances, {}
            return list(self.creation_order)

    def _end_close(self) -> None:
        with self.lock:
            retired, self._closing = self._closing, {}  # objects without a finalizer end here
            self.instances.clear()
            self.creation_order.clear()
        del retired  # after the lock: their __del__ may run now

    def close(self) -> None:
        order = self._begin_close()
        if order is None:
            return
        errors: list[BaseException] = []
        token = _CLOSING.set(self)  # the finalizers below are the closing code
        try:
            for slot in reversed(order):
                try:  # reading the finalizer may fail too: the other objects are still closed
                    instance, finalizer = self._take_for_finalizing(slot, self._sync_finalizer)
                    if instance is _UNSET:
                        continue
                    self._sync_finalize(instance, finalizer)
                except BaseException as exc:
                    errors.append(exc)
        finally:
            _CLOSING.reset(token)
        self._end_close()
        if errors:
            _raise_disposal_errors(errors)

    async def aclose(self) -> None:
        order = self._begin_close()
        if order is None:
            return
        errors: list[BaseException] = []
        token = _CLOSING.set(self)
        try:
            for slot in reversed(order):
                try:  # reading the finalizer may fail too: the other objects are still closed
                    instance, finalizer = self._take_for_finalizing(slot, self._async_finalizer)
                    if instance is _UNSET:
                        continue
                    await self._async_finalize(instance, finalizer)
                except BaseException as exc:
                    errors.append(exc)
        finally:
            _CLOSING.reset(token)
        self._end_close()
        if errors:
            _raise_disposal_errors(errors)


class Container:
    """Register and resolve dependencies using constructor injection.

    Filesystem-wide discovery is deliberately excluded from the safe core. An
    abstract service without an explicit implementation can only use one
    already-imported concrete subclass.
    """

    _provider_lock = _LibraryLock()
    _instance: Optional["Container"] = None

    def __init__(
        self,
        strict_interfaces: bool = False,
        auto_discover: bool = False,
        strict_registrations: bool = False,
        discovery_root: str | None = None,
    ) -> None:
        if auto_discover or discovery_root is not None:
            raise ValueError(
                "Filesystem auto-discovery is not part of the safe core API. "
                "Use py_autowired.auto_inject(container, root_dir=...) for module discovery."
            )
        self.registrations: Dict[Type[Any], ServiceRegistration] = {}
        self.strict_interfaces = strict_interfaces
        self.strict_registrations = strict_registrations
        # Seconds a thread may wait at a creation gate before a WARNING (once per wait).
        self.creation_wait_warning: float = 10.0
        self._implementation_selectors: Dict[Type[Any], Callable[[], Type[Any]]] = {}
        self.registration_history: list[tuple[str, Type[Any]]] = []
        self._registration_lock = _LibraryLock(reentrant=True)
        # Counts changes of the registrations: the active name map rebuilds itself when
        # it differs (a replace() after startup reaches new consumers without refresh()).
        self._changes = 0
        # A leaf lock for that count only: nothing else is taken and no user code runs under it,
        # so it joins no lock cycle - the count moves under any lock (a creation lock included).
        self._change_lock = threading.Lock()
        # Opt-in shutdown (close/aclose): container-created singletons in
        # creation order. Appended inside the existing per-registration lock;
        # no new lock on the resolution path.
        self._singleton_order: list[Any] = []
        self._keyed: Dict[tuple, ServiceRegistration] = {}
        # Earlier registrations of a type, in registration order. The current
        # one stays in ``registrations``; together they are what resolve_all returns.
        self._earlier: Dict[Type[Any], list[ServiceRegistration]] = {}
        self._open_generics: Dict[Any, tuple] = {}
        self._closed_generics: Dict[Any, ServiceRegistration] = {}
        self._closed = False
        self._current_scope_var: contextvars.ContextVar[Optional[_ScopeState]] = (
            contextvars.ContextVar(f"py_autowired_scope_{id(self)}", default=None)
        )
        self._resolution_stack: contextvars.ContextVar[tuple[Type[Any], ...]] = (
            contextvars.ContextVar(f"py_autowired_stack_{id(self)}", default=())
        )

    @classmethod
    def provider(cls, service_type: Type[T]) -> T:
        """Resolve from the process-wide default container."""

        if cls._instance is None:
            with cls._provider_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance.resolve(service_type)

    @classmethod
    def set_default(cls, container: "Container") -> None:
        """Replace the process-wide default container explicitly."""

        with cls._provider_lock:
            retired, cls._instance = cls._instance, container
        del retired  # after the lock: the old container's objects may run their __del__

    @property
    def current_scope(self) -> Optional[_ScopeState]:
        return self._current_scope_var.get()

    def set_primary(self, service_type: Type[Any]) -> None:
        """Mark a registration as preferred among name-matched candidates.

        Opt-in, like Spring's ``@Primary``: when several registrations match an
        ``*_instance`` field, primary ones are considered first. Without any
        primary mark, selection is unchanged. Call ``refresh()`` if autowiring
        has already cached a selection.
        """
        registration = self.registrations.get(service_type)
        if registration is None:
            raise ServiceNotRegisteredError(f"Service is not registered: {service_type!r}")
        registration.primary = True

    def is_registered(self, service_type: Type[Any]) -> bool:
        return (
            service_type in self.registrations
            or self._closed_generic_registration(service_type, cache=False) is not None
        )

    def register_open_generic(
        self,
        service_type: Any,
        implementation_type: Optional[Any] = None,
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> None:
        """Opt-in open generic registration (.NET ``AddTransient(typeof(I<>), ...)``).

        ``register_open_generic(Repository, SqlRepository)`` serves
        ``Repository[User]``, ``Repository[Order]``... on demand; each closed
        type has its own singleton/scoped slot. TypeVar constructor parameters of
        the implementation are substituted with the requested type arguments.
        Closed generics are not part of name-based field injection.
        """
        with self._registration_lock:
            if self.strict_registrations and service_type in self._open_generics:
                raise DuplicateServiceRegistrationError(
                    f"Open generic is already registered: {service_type.__name__}"
                )
            self._open_generics[service_type] = (implementation_type or service_type, scope)
            # Re-registration must not leave closed types built from the old one.
            for closed in [c for c in self._closed_generics.copy() if get_origin(c) is service_type]:
                del self._closed_generics[closed]
            self.registration_history.append(("open-generic", service_type))

    def _closed_generic_registration(
        self, service_type: Any, *, cache: bool = True
    ) -> Optional[ServiceRegistration]:
        origin = get_origin(service_type)
        if origin is None or origin not in self._open_generics:
            return None
        existing = self._closed_generics.get(service_type)
        if existing is not None:
            return existing
        implementation, scope = self._open_generics[origin]
        args = get_args(service_type)
        parameters = getattr(implementation, "__parameters__", ())
        registration = ServiceRegistration(
            service_type=service_type, implementation_type=implementation, scope=scope
        )
        if parameters and len(parameters) == len(args):
            registration.typevar_map = dict(zip(parameters, args))
        if not cache:
            return registration
        # No registration lock: a resolution may hold a creation lock here. setdefault is atomic;
        # the two readers that iterate the dict iterate a copy.
        return self._closed_generics.setdefault(service_type, registration)

    def set_implementation_selector(
        self, service_type: Type[Any], selector: Callable[[], Type[Any]]
    ) -> None:
        """Choose an abstraction's implementation at registration time.

        The selector runs on every registration that omits an explicit
        implementation, so configuration-driven providers can be swapped
        without importing every candidate.
        """

        if not inspect.isabstract(service_type):
            raise TypeError(
                f"An implementation selector needs an abstract type: {service_type.__name__}"
            )
        if not callable(selector):
            raise TypeError("An implementation selector must be callable")
        self._implementation_selectors[service_type] = selector

    def _implementation_for(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]]
    ) -> Optional[Type[Any]]:
        if implementation_type is not None:
            return implementation_type
        if not inspect.isabstract(service_type):
            if self.strict_interfaces:
                raise TypeError(
                    f"Concrete type cannot be registered in strict interface mode: {service_type.__name__}"
                )
            return service_type
        selector = self._implementation_selectors.get(service_type)
        if selector is not None:
            selected = selector()
            if selected is None:
                raise ResolutionError(
                    f"Implementation selector returned nothing for {service_type.__name__}"
                )
            if inspect.isabstract(selected):
                raise TypeError(
                    f"Selected implementation is not concrete: {selected.__name__}"
                )
            return selected
        return self._discovered_implementation(service_type, defer=True)

    @staticmethod
    def _discovered_implementation(service_type: Type[Any], *, defer: bool) -> Optional[Type[Any]]:
        # In a layered project the implementation usually lives in a module
        # that discovery imports only at startup, after the registrations. With
        # defer=True an interface registered before that keeps no implementation
        # yet; it is looked up again once modules are loaded or on first resolve.
        candidates = _concrete_subclasses(service_type)
        if not candidates:
            if defer:
                return None
            raise ResolutionError(
                f"No imported concrete implementation found for {service_type.__name__}"
            )
        if len(candidates) > 1:
            names = ", ".join(item.__name__ for item in candidates)
            raise AmbiguousImplementationError(
                f"Multiple implementations found for {service_type.__name__}: {names}"
            )
        return candidates[0]

    def _store_registration(
        self, registration: ServiceRegistration, *, policy: str = "register"
    ) -> bool:
        service_type = registration.service_type
        retired: list = []  # replaced registrations: let go after the lock (a last reference runs __del__)
        with self._registration_lock:
            exists = service_type in self.registrations
            if policy == "fallback" and exists:
                self.registration_history.append(("fallback-skipped", service_type))
                return False
            if policy == "replace" and not exists:
                raise MissingServiceReplacementError(
                    f"No registration exists to replace: {service_type.__name__}"
                )
            if policy == "register" and exists and self.strict_registrations:
                raise DuplicateServiceRegistrationError(
                    f"Service is already registered: {service_type.__name__}; use replace()"
                )
            if policy == "replace":
                retired.append(self._earlier.pop(service_type, None))
                # An alias shares a registration of the replaced type: it moves along
                # (a test replacing a service must not leave the old one behind an alias).
                for key, previous in list(self.registrations.items()):
                    if previous.service_type is service_type:  # the type itself is set just below
                        retired.append(previous)
                        self.registrations[key] = registration
            elif exists:  # the one written over is kept as earlier, or holds no object of the user
                self._keep_earlier(self.registrations[service_type], registration)
            self.registrations[service_type] = registration
            self._count_change()
            self.registration_history.append((policy, service_type))
        del retired
        return True

    def _count_change(self) -> None:
        """The registrations changed: the active name map follows (see _change_lock)."""
        with self._change_lock:
            self._changes += 1

    def _keep_earlier(self, previous: ServiceRegistration, registration: ServiceRegistration) -> None:
        """Keep the registration being taken over, like .NET keeps every Add*.

        The new registration answers resolve()/``x_instance`` as before; the
        earlier one stays reachable by its class name and through resolve_all().
        Registering the same class again replaces its earlier entry. An alias
        (a shared registration of another type) and an interface-only
        registration that never got an implementation are not kept.
        """
        service_type = registration.service_type
        kept = [item for item in self._earlier.get(service_type, ())
                if not _same_target(item, registration)]
        usable = previous.implementation_type is not None or previous.factory is not None
        if (previous.service_type is service_type and usable
                and not _same_target(previous, registration)):
            implementation = previous.implementation_type or service_type
            previous.variant = _Variant(implementation.__name__)
            kept.append(previous)
        if kept:
            self._earlier[service_type] = kept
        else:
            self._earlier.pop(service_type, None)

    def _registration(
        self,
        service_type: Type[Any],
        implementation_type: Optional[Type[Any]],
        scope: LifetimeScope,
    ) -> ServiceRegistration:
        return ServiceRegistration(
            service_type=service_type,
            implementation_type=self._implementation_for(service_type, implementation_type),
            scope=scope,
        )

    def register(
        self,
        service_type: Type[Any],
        implementation_type: Optional[Type[Any]] = None,
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> None:
        self._store_registration(self._registration(service_type, implementation_type, scope))

    def register_fallback(
        self,
        service_type: Type[Any],
        implementation_type: Optional[Type[Any]] = None,
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> bool:
        if self.is_registered(service_type):
            self.registration_history.append(("fallback-skipped", service_type))
            return False
        return self._store_registration(
            self._registration(service_type, implementation_type, scope), policy="fallback"
        )

    def replace(
        self,
        service_type: Type[Any],
        implementation_type: Optional[Type[Any]] = None,
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> None:
        self._store_registration(
            self._registration(service_type, implementation_type, scope), policy="replace"
        )

    def register_singleton(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]] = None
    ) -> None:
        self.register(service_type, implementation_type, LifetimeScope.SINGLETON)

    def register_singleton_fallback(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]] = None
    ) -> bool:
        return self.register_fallback(service_type, implementation_type, LifetimeScope.SINGLETON)

    def replace_singleton(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]] = None
    ) -> None:
        self.replace(service_type, implementation_type, LifetimeScope.SINGLETON)

    def register_scoped(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]] = None
    ) -> None:
        self.register(service_type, implementation_type, LifetimeScope.SCOPED)

    def register_transient(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]] = None
    ) -> None:
        self.register(service_type, implementation_type, LifetimeScope.TRANSIENT)

    def register_instance(self, service_type: Type[Any], instance: Any) -> None:
        self._store_registration(
            ServiceRegistration(
                service_type=service_type,
                implementation_type=type(instance),
                scope=LifetimeScope.SINGLETON,
                instance=instance,
                supplied=True,
            )
        )

    def register_alias(self, alias_type: Type[Any], target_type: Type[Any]) -> None:
        """Expose an existing registration under another service key.

        The alias and target share the exact registration, lifetime, scope state,
        and singleton instance.
        """

        with self._registration_lock:
            registration = self.registrations.get(target_type)
            if registration is None:
                raise ServiceNotRegisteredError(
                    f"Alias target is not registered: {target_type.__name__}"
                )
            if alias_type in self.registrations and self.strict_registrations:
                raise DuplicateServiceRegistrationError(
                    f"Service is already registered: {alias_type.__name__}"
                )
            retired = self.registrations.get(alias_type)  # let go after the lock (its __del__)
            self.registrations[alias_type] = registration
            self._count_change()
            self.registration_history.append(("alias", alias_type))
        del retired

    def register_factory(
        self,
        service_type: Type[Any],
        factory: Callable[["Container"], Any],
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> None:
        self._store_registration(
            ServiceRegistration(
                service_type=service_type,
                implementation_type=service_type,
                factory=factory,
                scope=scope,
            )
        )

    def create_scope(self) -> "Scope":
        return Scope(self)

    def _take_singletons_for_shutdown(self) -> list[Any]:
        if self._closed:
            return []
        self._closed = True
        ordered, seen = [], set()
        for instance in reversed(self._singleton_order):
            if id(instance) not in seen:  # one disposal per object identity
                seen.add(id(instance))
                ordered.append(instance)
        self._singleton_order.clear()
        return ordered

    def _complete_implementations(self) -> None:
        """Give interface-only registrations the implementation imported since.

        Called by auto_inject after discovery has imported the project, so the
        name map and validation see the implementation. Registrations whose
        implementation is still missing are left for resolve() to report.
        """
        with self._registration_lock:
            pending = [
                registration
                for registration in (*self.registrations.values(), *self._keyed.values())
                if registration.implementation_type is None
                and registration.factory is None
                and not registration.has_instance
                and inspect.isabstract(registration.service_type)
            ]
            for registration in pending:
                registration.implementation_type = self._discovered_implementation(
                    registration.service_type, defer=True
                )
                if registration.implementation_type is not None:
                    self._count_change()  # changed in place: the active name map follows
                earlier = self._earlier.get(registration.service_type)
                if earlier and self.registrations.get(registration.service_type) is registration:
                    kept = [item for item in earlier if not _same_target(item, registration)]
                    if kept:
                        self._earlier[registration.service_type] = kept
                    else:
                        del self._earlier[registration.service_type]

    def _detach_created_singletons(self) -> list[Any]:
        """Host shutdown: forget container-created singletons, stay usable.

        Used by the framework bridges when the application stops. Unlike
        close()/aclose(), the container is not closed: the next startup of the
        same application (tests often start one app several times) creates
        fresh singletons. Objects supplied through register_instance are never
        returned. The result is in disposal order (reverse creation order).
        """
        if self._closed:
            return []
        created, self._singleton_order = self._singleton_order, []
        created_ids = {id(instance) for instance in created}
        with self._registration_lock:
            registrations = (
                *self.registrations.values(),
                *self._keyed.values(),
                *self._closed_generics.copy().values(),
                *(item for items in self._earlier.values() for item in items),
            )
            for registration in registrations:
                if registration.has_instance and id(registration.instance) in created_ids:
                    registration.instance = _UNSET
        ordered: list[Any] = []
        seen: set[int] = set()
        for instance in reversed(created):
            if id(instance) not in seen:  # one disposal per object identity
                seen.add(id(instance))
                ordered.append(instance)
        return ordered

    def _release_singletons_at_exit(self) -> None:
        """Process-exit shutdown of the WSGI/ASGI bridges: like _release_singletons,
        but a singleton that can only be closed asynchronously is closed in a
        short event loop instead of failing with AsyncDisposalRequiredError."""
        errors: list[BaseException] = []
        for instance in self._detach_created_singletons():
            try:
                _ScopeState._sync_finalize(instance)
            except AsyncDisposalRequiredError:
                try:
                    asyncio.run(_ScopeState._async_finalize(instance))
                except BaseException as exc:
                    errors.append(exc)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _raise_disposal_errors(errors)

    def _release_singletons(self) -> None:
        """Synchronous host shutdown (WSGI bridges); see _detach_created_singletons."""
        errors: list[BaseException] = []
        for instance in self._detach_created_singletons():
            try:
                _ScopeState._sync_finalize(instance)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _raise_disposal_errors(errors)

    async def _arelease_singletons(self) -> None:
        """Asynchronous host shutdown (ASGI bridges); see _detach_created_singletons."""
        errors: list[BaseException] = []
        for instance in self._detach_created_singletons():
            try:
                await _ScopeState._async_finalize(instance)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _raise_disposal_errors(errors)

    def close(self) -> None:
        """Dispose container-created singletons in reverse creation order.

        Opt-in shutdown, modelled on .NET: objects supplied through
        ``register_instance`` belong to the caller and are not disposed. Call
        after application work has stopped; afterwards ``resolve`` raises
        ``ContainerClosedError``. Repeated calls are harmless.
        """
        errors: list[BaseException] = []
        for instance in self._take_singletons_for_shutdown():
            try:
                _ScopeState._sync_finalize(instance)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _raise_disposal_errors(errors)

    async def aclose(self) -> None:
        """Async variant of :meth:`close`; prefers ``aclose()`` finalizers."""
        errors: list[BaseException] = []
        for instance in self._take_singletons_for_shutdown():
            try:
                await _ScopeState._async_finalize(instance)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _raise_disposal_errors(errors)

    def resolve(self, service_type: Type[T]) -> T:
        registration = self.registrations.get(service_type)
        if registration is None:
            registration = self._closed_generic_registration(service_type)
        if registration is None:
            raise ServiceNotRegisteredError(f"Service is not registered: {service_type!r}")
        return self._resolve_registration(registration)

    def resolve_all(self, service_type: Type[T]) -> list[T]:
        """Every registration of ``service_type``, in registration order.

        Like .NET ``GetServices<T>()`` / Spring ``List<T>``: after
        ``register_scoped(IRepo, MemoryRepo)`` and ``register_scoped(IRepo, SqlRepo)``
        it returns one MemoryRepo and one SqlRepo, each with its own lifetime.
        An unregistered type gives an empty list.
        """
        current = self.registrations.get(service_type)
        if current is None:
            current = self._closed_generic_registration(service_type)
        registrations = list(self._earlier.get(service_type, ()))
        if current is not None:
            registrations.append(current)
        return [self._resolve_registration(registration) for registration in registrations]

    def register_keyed(
        self,
        service_type: Type[Any],
        key: Any,
        implementation_type: Optional[Type[Any]] = None,
        scope: LifetimeScope = LifetimeScope.TRANSIENT,
    ) -> None:
        """Opt-in keyed registration (.NET keyed services / Spring ``@Qualifier``).

        Several implementations of one type can be registered under different
        hashable keys. Keyed registrations are not part of name-based field
        injection; resolve them with :meth:`resolve_keyed` or a constructor
        parameter annotated ``Annotated[T, Keyed(key)]``.
        """
        registration = self._registration(service_type, implementation_type, scope)
        registration.key = key
        with self._registration_lock:
            if self.strict_registrations and (service_type, key) in self._keyed:
                raise DuplicateServiceRegistrationError(
                    f"Keyed service is already registered: {service_type.__name__} with key {key!r}"
                )
            self._keyed[(service_type, key)] = registration
            self.registration_history.append(("keyed", service_type))

    def resolve_keyed(self, service_type: Type[T], key: Any) -> T:
        registration = self._keyed.get((service_type, key))
        if registration is None:
            raise ServiceNotRegisteredError(
                f"Keyed service is not registered: {service_type!r} with key {key!r}"
            )
        return self._resolve_registration(registration)

    def is_keyed_registered(self, service_type: Type[Any], key: Any) -> bool:
        return (service_type, key) in self._keyed

    @staticmethod
    def _note_owner(registration: ServiceRegistration, me: int) -> None:
        with _WAIT_LOCK:
            owner = _LOCK_OWNERS.get(id(registration))
            depth = owner[1] + 1 if owner is not None and owner[0] == me else 1
            _LOCK_OWNERS[id(registration)] = (me, depth)

    def _acquire_creation(self, registration: ServiceRegistration) -> None:
        """Acquire a singleton's creation lock; raise instead of deadlocking.

        Uncontended (and re-entrant) acquisition is immediate. Only a contended
        first creation polls and checks the cross-thread wait-for graph.
        """
        me = threading.get_ident()
        if registration.lock.acquire(blocking=False):
            self._note_owner(registration, me)
            _hold()
            return
        with _WAIT_LOCK:
            _WAITING[me] = registration
        started, warned = time.monotonic(), False
        try:
            while True:
                if registration.lock.acquire(timeout=0.05):
                    self._note_owner(registration, me)
                    _hold()
                    return
                if not warned and time.monotonic() - started >= self.creation_wait_warning:
                    warned = True
                    self._warn_long_wait(registration, me, time.monotonic() - started)
                cycle = _thread_wait_cycle(me)
                if cycle is not None:
                    names = " -> ".join(_slot_name(item.slot) for item in cycle)
                    raise CircularDependencyError(
                        f"Circular dependency across threads: {names} -> "
                        f"{_slot_name(cycle[0].slot)}"
                    )
        finally:
            with _WAIT_LOCK:
                _WAITING.pop(me, None)

    @staticmethod
    def _warn_long_wait(registration: Any, me: int, waited: float) -> None:
        """Once per wait: no lock shows a wait such as thread.join(), so a deadlock
        through one cannot be detected - but it must not stay silent either. The
        background writer logs it now, even while this thread stays stuck."""
        with _WAIT_LOCK:
            owner = _LOCK_OWNERS.get(id(registration))
        _BACKGROUND_LOG.put(
            "thread %s has waited %.1fs for the creation of %s held by thread %s; "
            "if that thread waits for this one (for example thread.join()), this is a deadlock",
            (me, waited, _slot_name(registration.slot), owner[0] if owner else "?"))

    @staticmethod
    def _release_creation(registration: ServiceRegistration) -> None:
        me = threading.get_ident()
        with _WAIT_LOCK:  # update ownership BEFORE releasing: never a stale owner
            owner = _LOCK_OWNERS.get(id(registration))
            mine = owner is not None and owner[0] == me  # this thread noted it: it counts as held
            if mine:
                if owner[1] <= 1:
                    del _LOCK_OWNERS[id(registration)]
                else:
                    _LOCK_OWNERS[id(registration)] = (me, owner[1] - 1)
        registration.lock.release()
        if mine:
            _unhold()

    def _resolve_registration(self, registration: ServiceRegistration) -> Any:
        instance = registration.instance  # read once: a host stop may clear it meanwhile
        if instance is not _UNSET and registration.scope is LifetimeScope.SINGLETON and not self._closed:
            return instance  # a ready singleton: no lock taken, nothing to warn about
        service_type = registration.service_type
        if self._closed:
            raise ContainerClosedError("The dependency container is closed")
        if registration.scope == LifetimeScope.SINGLETON:
            if registration.instance is _UNSET:  # ready singletons: no lock at all
                self._acquire_creation(registration)
                try:
                    if registration.instance is _UNSET:
                        token = _SINGLETON_IN_CREATION.set(_slot_name(registration.slot))
                        try:
                            instance = self._create_instance(registration)
                        finally:
                            _SINGLETON_IN_CREATION.reset(token)
                        self._singleton_order.append(instance)
                        registration.instance = instance
                finally:
                    self._release_creation(registration)
            return registration.instance
        if registration.scope == LifetimeScope.SCOPED:
            scope = self.current_scope
            if scope is None:
                raise ScopeNotActiveError(
                    f"Scoped service requires an active scope: {service_type.__name__}"
                    + _ThreadBirth.refusal_note()
                )
            owner = _SINGLETON_IN_CREATION.get()
            if owner is not None:
                scoped = _slot_name(registration.slot)
                _warn_once(("captures", owner, scoped),
                           "%s (scoped) was resolved while singleton %s was being created; "
                           "if %s keeps it, it outlives its scope", scoped, owner, owner)
            return scope.get_or_create(registration, self)
        return self._create_instance(registration)

    def _create_instance(self, registration: ServiceRegistration) -> Any:
        service_type = registration.service_type
        slot = registration.slot  # == service_type unless the registration is keyed
        stack = self._resolution_stack.get()
        if slot in stack:
            start = stack.index(slot)
            cycle = stack[start:] + (slot,)
            raise CircularDependencyError(
                "Circular dependency: " + " -> ".join(_slot_name(item) for item in cycle)
            )
        token = self._resolution_stack.set(stack + (slot,))
        try:
            if registration.factory is not None:
                instance = registration.factory(self)
                if inspect.iscoroutine(instance):
                    # A bare coroutine can never be a usable service. Other
                    # awaitables (pools, futures) are legitimate and untouched.
                    instance.close()
                    raise AsyncFactoryError(
                        f"Factory for {service_type.__name__} returned a coroutine; "
                        "synchronous resolve() needs a service instance"
                    )
                return instance
            implementation = registration.implementation_type
            if implementation is None:
                if not inspect.isabstract(service_type):
                    raise ResolutionError(f"Registration has no implementation: {service_type.__name__}")
                # Interface registered before its implementation was imported.
                implementation = self._discovered_implementation(service_type, defer=False)
                # A plain store under no registration lock: a creation lock may be held here, and the
                # library never takes the registration lock under one. The count's lock is a leaf.
                registration.implementation_type = implementation
                self._count_change()  # changed in place: the active name map follows
            # auto_inject fills these fields after construction, so a class that
            # only uses field injection must not have its __init__ resolved. One
            # that also takes constructor dependencies still gets them.
            own = getattr(implementation, "__di_own_attrs__", None)  # _field_injection_only, inline
            if own is None:
                own = getattr(implementation, "__di_attrs__", ())
            if own and _accepts_no_arguments(implementation):
                return implementation()
            signature = _init_signature(implementation)
            hints, extras, hint_error = _constructor_hints(implementation.__init__)
            if hint_error is not None:
                _warn_once(
                    ("hints", implementation.__module__, implementation.__qualname__),
                    "%s.%s: type hints cannot be resolved (%s: %s); a parameter with a "
                    "default keeps its default, one without fails to resolve",
                    implementation.__module__, implementation.__qualname__,
                    type(hint_error).__name__, hint_error,
                )
            parameters = list(signature.parameters.values())[1:]
            arguments: dict[str, Any] = {}
            for parameter in parameters:
                if parameter.kind in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }:
                    continue
                if (hint_error is not None and parameter.default is not inspect.Parameter.empty
                        and isinstance(parameter.annotation, str) and "Keyed(" in parameter.annotation):
                    _warn_once(
                        ("keyed-default", implementation.__module__, implementation.__qualname__,
                         parameter.name),
                        "%s.%s.%s: Keyed type hint %s cannot be resolved (%s: %s); the default "
                        "value is used even if that keyed service is registered",
                        implementation.__module__, implementation.__qualname__, parameter.name,
                        parameter.annotation, type(hint_error).__name__, hint_error,
                    )
                hint = extras.get(parameter.name, parameter.annotation)
                keyed = _keyed_marker(hint)
                if keyed is None and extras is not hints:  # only a hint with metadata can hold one
                    unsupported = _unsupported_keyed(implementation, parameter, hint)
                    if unsupported is not None:
                        _warn_once(("unsupported-keyed", implementation.__module__,
                                    implementation.__qualname__, parameter.name),
                                   "%s.%s", implementation.__module__, unsupported)
                if keyed is not None:
                    keyed_type, key = keyed
                    if self.is_keyed_registered(keyed_type, key):
                        arguments[parameter.name] = self.resolve_keyed(keyed_type, key)
                    elif parameter.default is inspect.Parameter.empty:
                        raise ServiceNotRegisteredError(
                            f"{implementation.__name__}.{parameter.name} requires keyed "
                            f"{keyed_type!r} with key {key!r}"
                        )
                    continue
                annotation = _unwrap_annotation(_parameter_hint(parameter, hints))
                if registration.typevar_map and isinstance(annotation, TypeVar):
                    annotation = registration.typevar_map.get(annotation, annotation)
                if annotation is inspect.Parameter.empty:
                    if parameter.default is inspect.Parameter.empty:
                        raise ResolutionError(
                            f"{implementation.__name__}.{parameter.name} needs a type annotation or default"
                        )
                    continue
                deferred = _deferred_wrapper(annotation)
                if deferred is not None:
                    wrapper, inner = deferred
                    arguments[parameter.name] = wrapper(self, inner)
                elif self.is_registered(annotation):
                    arguments[parameter.name] = self.resolve(annotation)
                elif parameter.default is inspect.Parameter.empty:
                    raise ServiceNotRegisteredError(
                        f"{implementation.__name__}.{parameter.name} requires unregistered {annotation!r}"
                    )
            positional = [p for p in parameters if p.kind is inspect.Parameter.POSITIONAL_ONLY]
            if positional:
                # Positional-only parameters go by position: a skipped one is passed
                # its own default (a skipped one never lacks a default: that fails above).
                values = [arguments.pop(p.name, p.default) for p in positional]
                return implementation(*values, **arguments)
            return implementation(**arguments)
        finally:
            self._resolution_stack.reset(token)

    def scoped_function(self, function: Callable[..., T]) -> Callable[..., T]:
        """Run a sync or async callable inside a fresh dependency scope."""

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any):
                async with self.create_scope():
                    return await function(*args, **kwargs)

            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any):
            with self.create_scope():
                return function(*args, **kwargs)

        return sync_wrapper


class Scope:
    """A sync and async context manager for scoped service lifetimes."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.state = _ScopeState(container)
        self._token: Optional[contextvars.Token[Optional[_ScopeState]]] = None

    def __enter__(self) -> "Scope":
        if self._token is not None:
            raise ScopeNotActiveError("A scope object cannot be entered twice")
        self.state.thread = threading.current_thread()
        self._token = self.container._current_scope_var.set(self.state)
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        try:
            self.state.close()
        finally:
            self._leave()

    async def __aenter__(self) -> "Scope":
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        try:
            await self.state.aclose()
        finally:
            self._leave()

    def _leave(self) -> None:
        """Make the scope that was active before this one current again."""
        if self._token is not None:
            self.container._current_scope_var.reset(self._token)
            self._token = None

    def resolve(self, service_type: Type[T]) -> T:
        return self.container.resolve(service_type)

    def resolve_keyed(self, service_type: Type[T], key: Any) -> T:
        return self.container.resolve_keyed(service_type, key)

    def resolve_all(self, service_type: Type[T]) -> list[T]:
        return self.container.resolve_all(service_type)


# ---------------------------------------------------------------------------------------------
# Thread birth on the free-threaded build: no library context value is inherited.
#
# With sys.flags.thread_inherit_context (the default of the free-threaded 3.14 build) a new thread
# starts with a copy of its starter's context. A pool worker born inside one request then keeps
# that request's scope for its whole life and serves other requests from it - silently, another
# request's objects. A thread's birth here leaves the library's own context values (every
# ContextVar named "py_autowired...") out of that copy, so a thread sees what it sees on 3.11.
# Context handed over explicitly (copy_context().run, asyncio.to_thread, anyio,
# Thread(context=...)) is not touched, and neither is any other library's value.
#
# How: a sys.monitoring PY_START event local to threading.Thread.start's code object - only that
# one function; the resolution path is unchanged. It is checked when installed (a probe thread must
# not inherit a library value). If it cannot be set up, or fails later, nothing is left silent: a
# hard rule reads a scope only in the thread that entered it - every other thread is refused,
# explicit hand-overs (asyncio.to_thread, anyio) included - for the rest of the process. The
# import-time case logs one WARNING; every refusal names the reason in its ScopeNotActiveError.
# ---------------------------------------------------------------------------------------------

def _fill_context(items) -> None:
    for var, value in items:
        var.set(value)


class _ThreadBirth:
    PREFIX = "py_autowired"
    _TOOL_IDS = (4, 3)  # not reserved by Python (0 debugger, 1 coverage, 2 profiler, 5 optimizer)
    _TOOL_NAME = "py_autowired thread birth"
    SELF_TEST_TIMEOUT = 2.0  # seconds; the probe normally finishes in well under a millisecond
    # The documented status: "active" (the hook is set and checked), "hard-rule" (it could not be,
    # or failed later - one way, for the rest of the process), "not-needed" (threads do not inherit
    # their starter's context on this build).
    STATUS = "not-needed"
    _decided = False
    _tool = None
    _original_current_scope = None
    _lock = _LibraryLock()  # a fall-back and the installation's success never interleave

    @staticmethod
    def refusal_note() -> str:
        if _ThreadBirth.STATUS != "hard-rule":
            return ""
        return (" (free-threaded build: py_autowired cannot stop new threads from inheriting its "
                "context here - its thread-birth hook could not be set up or failed (STATUS="
                "hard-rule) - so a scope is read only in the thread that entered it; explicit "
                "hand-overs such as asyncio.to_thread and anyio are refused too. See Known limits)")

    @staticmethod
    def _clean_copy() -> contextvars.Context:
        """A copy of the current context without the library's values."""
        current = contextvars.copy_context()
        keep = [(var, value) for var, value in current.items()
                if not var.name.startswith(_ThreadBirth.PREFIX)]
        if len(keep) == len(current):
            return current
        clean = contextvars.Context()
        clean.run(_fill_context, keep)
        return clean

    @staticmethod
    def _on_thread_start(code, instruction_offset) -> None:
        # Runs in the starting thread at the top of Thread.start(), before the inherited copy is made.
        try:
            thread = sys._getframe(1).f_locals.get("self")
            if isinstance(thread, threading.Thread) and thread._context is None:
                thread._context = _ThreadBirth._clean_copy()  # no context= given: the cleaned copy
        except Exception:  # never breaks the start, never logs here (a lock may be held): this
            try:          # thread may inherit a scope, so the hard rule takes over for good
                _ThreadBirth._go_hard()
            except Exception:
                pass

    @staticmethod
    def _self_test() -> bool:
        """A probe thread must not see a library value and must still see any other value."""
        library = contextvars.ContextVar(_ThreadBirth.PREFIX + "_self_test", default=None)
        other = contextvars.ContextVar("thread_birth_self_test", default=None)
        seen: Dict[str, Any] = {}

        def body() -> None:
            library.set("set")
            other.set("set")
            probe = threading.Thread(target=lambda: seen.update(library=library.get(), other=other.get()),
                                     name="py_autowired thread-birth self-test", daemon=True)
            probe.start()
            probe.join(_ThreadBirth.SELF_TEST_TIMEOUT)  # one that never runs leaves "seen" empty: failed
        contextvars.Context().run(body)  # cannot start a thread now: _install catches it
        return seen == {"library": None, "other": "set"}

    @staticmethod
    def _hard_current_scope(container: "Container") -> Optional[_ScopeState]:
        """The hard rule's scope read: a scope is read only in the thread that entered it."""
        state = container._current_scope_var.get()
        if state is None or state.thread is threading.current_thread():
            return state
        return None  # the refused read names the reason in its ScopeNotActiveError

    @staticmethod
    def _apply_hard_rule() -> None:
        if _ThreadBirth._original_current_scope is None:
            _ThreadBirth._original_current_scope = Container.current_scope
        Container.current_scope = property(_ThreadBirth._hard_current_scope)  # type: ignore[assignment]

    @staticmethod
    def _restore_scope_read() -> None:
        if _ThreadBirth._original_current_scope is not None:
            Container.current_scope = _ThreadBirth._original_current_scope  # type: ignore[assignment]
            _ThreadBirth._original_current_scope = None

    @staticmethod
    def _go_hard() -> str:
        """The hard rule, for the rest of the process (no way back)."""
        with _ThreadBirth._lock:
            _ThreadBirth._apply_hard_rule()
            _ThreadBirth.STATUS = "hard-rule"
            return _ThreadBirth.STATUS

    _IMPORT_WARNING = (
        "py_autowired could not set up its thread-birth hook on this free-threaded build (no free "
        "sys.monitoring tool id among %s, the name is already taken by another copy, or its check "
        "failed): STATUS=hard-rule - a scope is read only in the thread that entered it, explicit "
        "hand-overs (asyncio.to_thread, anyio) are refused too, for the rest of the process. See "
        "Known limits")

    @staticmethod
    def _install() -> str:
        """Decided once, when the package is imported; returns the status. Never raises."""
        if not getattr(sys.flags, "thread_inherit_context", 0):
            _ThreadBirth.STATUS = "not-needed"
            return _ThreadBirth.STATUS
        if _ThreadBirth._decided:
            return _ThreadBirth.STATUS
        _ThreadBirth._decided = True
        _ThreadBirth._apply_hard_rule()  # until the hook is set and checked
        try:
            monitoring = sys.monitoring
            # The name already held (another copy of the library, or foreign code): not ours.
            if not any(monitoring.get_tool(tool) == _ThreadBirth._TOOL_NAME for tool in range(6)):
                for candidate in _ThreadBirth._TOOL_IDS:
                    if monitoring.get_tool(candidate) is not None:
                        continue
                    try:
                        monitoring.use_tool_id(candidate, _ThreadBirth._TOOL_NAME)
                    except ValueError:  # taken meanwhile
                        continue
                    _ThreadBirth._tool = candidate
                    break
            # Another copy took an id under the name at the same moment: whoever sees the other after
            # taking its own id gives way (the later check always sees the earlier claim, so never
            # two active; at worst both keep the hard rule).
            if _ThreadBirth._tool is not None and any(
                    monitoring.get_tool(tool) == _ThreadBirth._TOOL_NAME
                    for tool in range(6) if tool != _ThreadBirth._tool):
                _ThreadBirth._uninstall()
            if _ThreadBirth._tool is not None:
                tool = _ThreadBirth._tool
                monitoring.register_callback(tool, monitoring.events.PY_START, _ThreadBirth._on_thread_start)
                monitoring.set_local_events(tool, threading.Thread.start.__code__, monitoring.events.PY_START)
                if _ThreadBirth._self_test():
                    with _ThreadBirth._lock:
                        if _ThreadBirth.STATUS != "hard-rule":
                            _ThreadBirth._restore_scope_read()
                            _ThreadBirth.STATUS = "active"
                            return _ThreadBirth.STATUS
                    # The hook failed meanwhile (another thread): a fall-back at import, said below.
        except Exception:  # e.g. Thread.start wrapped by something without __code__
            pass
        _ThreadBirth._uninstall()
        _ThreadBirth._go_hard()
        try:  # at import no library lock is held: the one WARNING, written here (no thread, no queue)
            logger.warning(_ThreadBirth._IMPORT_WARNING, list(_ThreadBirth._TOOL_IDS))
        except Exception:  # the import never fails because of it; STATUS still says it
            pass
        return _ThreadBirth.STATUS

    @staticmethod
    def _uninstall() -> None:
        """Remove the hook and give the tool id back (a failed installation; tests). Each step on
        its own: one that fails never keeps the tool id."""
        tool = _ThreadBirth._tool
        if tool is None:
            return
        _ThreadBirth._tool = None
        monitoring = sys.monitoring
        try:
            monitoring.set_local_events(tool, threading.Thread.start.__code__, 0)
        except Exception:
            pass
        try:
            monitoring.register_callback(tool, monitoring.events.PY_START, None)
        except Exception:
            pass
        try:
            monitoring.free_tool_id(tool)
        except Exception:
            pass

    @staticmethod
    def _reset_for_tests() -> None:
        """Tests only: forget the decision (the library itself never installs twice)."""
        _ThreadBirth._uninstall()
        _ThreadBirth._restore_scope_read()
        _ThreadBirth._decided = False
        _ThreadBirth.STATUS = "not-needed"
