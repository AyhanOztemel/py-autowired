"""Framework-independent dependency injection container."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
import inspect
import threading
from types import TracebackType
from typing import Annotated, Any, Callable, Dict, Optional, Type, TypeVar, get_args, get_origin, get_type_hints


T = TypeVar("T")
_UNSET = object()


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


class ResolutionError(ValueError, DependencyInjectionError):
    """Raised when a service cannot be constructed."""


class AmbiguousImplementationError(DependencyInjectionError):
    """Raised when multiple loaded implementations match an abstraction."""


class AsyncDisposalRequiredError(DependencyInjectionError):
    """Raised when an async resource is closed through a synchronous scope."""


@dataclass
class ServiceRegistration:
    service_type: Type[Any]
    implementation_type: Optional[Type[Any]] = None
    factory: Optional[Callable[["Container"], Any]] = None
    scope: LifetimeScope = LifetimeScope.TRANSIENT
    instance: Any = _UNSET
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def has_instance(self) -> bool:
        return self.instance is not _UNSET


def factory_dependencies(*service_types: Type[Any]):
    """Declare a factory's hidden dependencies for graph validation."""

    def decorate(factory: Callable[["Container"], Any]):
        setattr(factory, "__di_dependencies__", tuple(service_types))
        return factory

    return decorate


def _is_abstract_strict(service_type: Type[Any]) -> bool:
    return inspect.isabstract(service_type)


def _is_abstract_loose(service_type: Type[Any]) -> bool:
    """Also treat leftover ``__abstractmethods__`` as a contract.

    Classes built without ABCMeta -- or rebuilt by a decorator -- can carry
    abstract methods that ``inspect.isabstract`` misses.
    """

    return inspect.isabstract(service_type) or bool(
        getattr(service_type, "__abstractmethods__", ())
    )


_ABSTRACT_DETECTORS = {"strict": _is_abstract_strict, "loose": _is_abstract_loose}


def _concrete_subclasses(
    service_type: Type[Any],
    is_abstract: Callable[[Type[Any]], bool] = _is_abstract_strict,
) -> list[Type[Any]]:
    found: list[Type[Any]] = []
    seen: set[Type[Any]] = set()

    def visit(candidate: Type[Any]) -> None:
        for child in candidate.__subclasses__():
            if child in seen:
                continue
            seen.add(child)
            if not is_abstract(child):
                found.append(child)
            visit(child)

    visit(service_type)
    return found


def _unwrap_annotation(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


class _ScopeState:
    def __init__(self) -> None:
        self.instances: Dict[Type[Any], Any] = {}
        self.creation_order: list[Type[Any]] = []
        self.lock = threading.RLock()
        self.closed = False

    def get_or_create(self, registration: ServiceRegistration, container: "Container") -> Any:
        service_type = registration.service_type
        instance = self.instances.get(service_type, _UNSET)
        if instance is not _UNSET:
            return instance
        with self.lock:
            if self.closed:
                raise ScopeNotActiveError("The active dependency scope is already closed")
            instance = self.instances.get(service_type, _UNSET)
            if instance is _UNSET:
                instance = container._create_instance(registration)
                self.instances[service_type] = instance
                self.creation_order.append(service_type)
            return instance

    @staticmethod
    def _sync_finalize(instance: Any) -> None:
        finalizer = getattr(instance, "dispose", None) or getattr(instance, "close", None)
        if not callable(finalizer):
            if callable(getattr(instance, "aclose", None)):
                raise AsyncDisposalRequiredError(
                    f"{type(instance).__name__} requires 'async with container.create_scope()'"
                )
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
    async def _async_finalize(instance: Any) -> None:
        finalizer = (
            getattr(instance, "aclose", None)
            or getattr(instance, "dispose", None)
            or getattr(instance, "close", None)
        )
        if not callable(finalizer):
            return
        result = finalizer()
        if inspect.isawaitable(result):
            await result

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for service_type in reversed(self.creation_order):
            try:
                self._sync_finalize(self.instances[service_type])
            except BaseException as exc:
                errors.append(exc)
        self.instances.clear()
        self.creation_order.clear()
        if errors:
            raise errors[0]

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for service_type in reversed(self.creation_order):
            try:
                await self._async_finalize(self.instances[service_type])
            except BaseException as exc:
                errors.append(exc)
        self.instances.clear()
        self.creation_order.clear()
        if errors:
            raise errors[0]


class Container:
    """Register and resolve dependencies using constructor injection.

    Filesystem-wide discovery is deliberately excluded from the safe core. An
    abstract service without an explicit implementation can only use one
    already-imported concrete subclass.
    """

    _provider_lock = threading.Lock()
    _instance: Optional["Container"] = None

    def __init__(
        self,
        strict_interfaces: bool = False,
        auto_discover: bool = False,
        strict_registrations: bool = False,
        discovery_root: str | None = None,
        ambient_scope: bool = False,
        abstract_detection: str = "strict",
    ) -> None:
        if auto_discover or discovery_root is not None:
            raise ValueError(
                "Filesystem auto-discovery is not part of the safe core API. "
                "Import implementations explicitly or use py_autowired.legacy."
            )
        if abstract_detection not in _ABSTRACT_DETECTORS:
            raise ValueError(
                f"abstract_detection must be 'strict' or 'loose', got {abstract_detection!r}"
            )
        self.registrations: Dict[Type[Any], ServiceRegistration] = {}
        self.strict_interfaces = strict_interfaces
        self.strict_registrations = strict_registrations
        self.ambient_scope = ambient_scope
        self.abstract_detection = abstract_detection
        self._is_abstract = _ABSTRACT_DETECTORS[abstract_detection]
        self._implementation_selectors: Dict[Type[Any], Callable[[], Type[Any]]] = {}
        self.registration_history: list[tuple[str, Type[Any]]] = []
        self._registration_lock = threading.RLock()
        self._current_scope_var: contextvars.ContextVar[Optional[_ScopeState]] = (
            contextvars.ContextVar(f"py_autowired_scope_{id(self)}", default=None)
        )
        self._ambient_scope_var: contextvars.ContextVar[Optional[_ScopeState]] = (
            contextvars.ContextVar(f"py_autowired_ambient_{id(self)}", default=None)
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
            cls._instance = container

    @property
    def current_scope(self) -> Optional[_ScopeState]:
        return self._current_scope_var.get()

    def is_registered(self, service_type: Type[Any]) -> bool:
        return service_type in self.registrations

    def _active_scope(self) -> Optional[_ScopeState]:
        scope = self._current_scope_var.get()
        if scope is not None or not self.ambient_scope:
            return scope
        # An ambient scope has no exit point, so nothing closes it. Callers that
        # need finalization must wrap the work in scoped_function or a Scope.
        scope = self._ambient_scope_var.get()
        if scope is None:
            scope = _ScopeState()
            self._ambient_scope_var.set(scope)
        return scope

    def set_implementation_selector(
        self, service_type: Type[Any], selector: Callable[[], Type[Any]]
    ) -> None:
        """Choose an abstraction's implementation at registration time.

        The selector runs on every registration that omits an explicit
        implementation, so configuration-driven providers can be swapped
        without importing every candidate.
        """

        if not self._is_abstract(service_type):
            raise TypeError(
                f"An implementation selector needs an abstract type: {service_type.__name__}"
            )
        if not callable(selector):
            raise TypeError("An implementation selector must be callable")
        self._implementation_selectors[service_type] = selector

    def _implementation_for(
        self, service_type: Type[Any], implementation_type: Optional[Type[Any]]
    ) -> Type[Any]:
        if implementation_type is not None:
            return implementation_type
        if not self._is_abstract(service_type):
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
            if self._is_abstract(selected):
                raise TypeError(
                    f"Selected implementation is not concrete: {selected.__name__}"
                )
            return selected
        candidates = _concrete_subclasses(service_type, self._is_abstract)
        if not candidates:
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
            self.registrations[service_type] = registration
            self.registration_history.append((policy, service_type))
            return True

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
            self.registrations[alias_type] = registration
            self.registration_history.append(("alias", alias_type))

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

    def resolve(self, service_type: Type[T]) -> T:
        registration = self.registrations.get(service_type)
        if registration is None:
            raise ServiceNotRegisteredError(f"Service is not registered: {service_type!r}")
        if registration.scope == LifetimeScope.SINGLETON:
            if registration.instance is _UNSET:
                with registration.lock:
                    if registration.instance is _UNSET:
                        registration.instance = self._create_instance(registration)
            return registration.instance
        if registration.scope == LifetimeScope.SCOPED:
            scope = self._active_scope()
            if scope is None:
                raise ScopeNotActiveError(
                    f"Scoped service requires an active scope: {service_type.__name__}"
                )
            return scope.get_or_create(registration, self)
        return self._create_instance(registration)

    def _create_instance(self, registration: ServiceRegistration) -> Any:
        service_type = registration.service_type
        stack = self._resolution_stack.get()
        if service_type in stack:
            start = stack.index(service_type)
            cycle = stack[start:] + (service_type,)
            raise CircularDependencyError(
                "Circular dependency: " + " -> ".join(item.__name__ for item in cycle)
            )
        token = self._resolution_stack.set(stack + (service_type,))
        try:
            if registration.factory is not None:
                return registration.factory(self)
            implementation = registration.implementation_type
            if implementation is None:
                raise ResolutionError(f"Registration has no implementation: {service_type.__name__}")
            # Classes marked by auto_inject receive their dependencies as
            # attributes after construction, so their __init__ must stay untouched.
            if getattr(implementation, "__di_attrs__", ()):
                return implementation()
            try:
                signature = inspect.signature(implementation.__init__)
                hints = get_type_hints(implementation.__init__)
            except (NameError, TypeError, ValueError):
                signature = inspect.signature(implementation.__init__)
                hints = {}
            arguments: dict[str, Any] = {}
            for parameter in list(signature.parameters.values())[1:]:
                if parameter.kind in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }:
                    continue
                annotation = _unwrap_annotation(hints.get(parameter.name, parameter.annotation))
                if annotation is inspect.Parameter.empty:
                    if parameter.default is inspect.Parameter.empty:
                        raise ResolutionError(
                            f"{implementation.__name__}.{parameter.name} needs a type annotation or default"
                        )
                    continue
                if self.is_registered(annotation):
                    arguments[parameter.name] = self.resolve(annotation)
                elif parameter.default is inspect.Parameter.empty:
                    raise ServiceNotRegisteredError(
                        f"{implementation.__name__}.{parameter.name} requires unregistered {annotation!r}"
                    )
            return implementation(**arguments)
        finally:
            self._resolution_stack.reset(token)

    def scoped_function(self, function: Callable[..., T]) -> Callable[..., T]:
        """Run a sync or async callable inside a fresh dependency scope."""

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any):
                if self._reuses_outer_scope():
                    return await function(*args, **kwargs)
                async with self.create_scope():
                    return await function(*args, **kwargs)

            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any):
            if self._reuses_outer_scope():
                return function(*args, **kwargs)
            with self.create_scope():
                return function(*args, **kwargs)

        return sync_wrapper

    def _reuses_outer_scope(self) -> bool:
        """Ambient mode shares one scope per context instead of nesting."""

        if not self.ambient_scope:
            return False
        return (
            self._current_scope_var.get() is not None
            or self._ambient_scope_var.get() is not None
        )


class Scope:
    """A sync and async context manager for scoped service lifetimes."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.state = _ScopeState()
        self._token: Optional[contextvars.Token[Optional[_ScopeState]]] = None

    def __enter__(self) -> "Scope":
        if self._token is not None:
            raise ScopeNotActiveError("A scope object cannot be entered twice")
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
            if self._token is not None:
                self.container._current_scope_var.reset(self._token)
                self._token = None

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
            if self._token is not None:
                self.container._current_scope_var.reset(self._token)
                self._token = None

    def resolve(self, service_type: Type[T]) -> T:
        return self.container.resolve(service_type)
