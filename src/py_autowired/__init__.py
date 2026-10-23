"""Public API for py-autowired."""

from importlib.metadata import PackageNotFoundError, version as _version

from .autowired import (
    AutoInjectionResolutionError,
    AutowiringValidationError,
    activate_container,
    check_skipped,
    auto_inject,
    get_cache_stats,
    print_cache_stats,
    refresh,
    skipped_during_discovery,
    validate_autowiring,
)
from .container import (
    AmbiguousImplementationError,
    AsyncDisposalRequiredError,
    AsyncFactoryError,
    ContainerClosedError,
    Keyed,
    Lazy,
    Provider,
    CircularDependencyError,
    Container,
    DependencyInjectionError,
    DuplicateServiceRegistrationError,
    LifetimeScope,
    MissingServiceReplacementError,
    ResolutionError,
    Scope,
    ScopeNotActiveError,
    ServiceNotRegisteredError,
    ServiceRegistration,
    factory_dependencies,
)

try:
    __version__ = _version("py-autowired")
except PackageNotFoundError:  # running straight from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "AmbiguousImplementationError",
    "AsyncDisposalRequiredError",
    "AsyncFactoryError",
    "ContainerClosedError",
    "Keyed",
    "Lazy",
    "Provider",
    "AutoInjectionResolutionError",
    "AutowiringValidationError",
    "CircularDependencyError",
    "Container",
    "DependencyInjectionError",
    "DuplicateServiceRegistrationError",
    "LifetimeScope",
    "MissingServiceReplacementError",
    "ResolutionError",
    "Scope",
    "ScopeNotActiveError",
    "ServiceNotRegisteredError",
    "ServiceRegistration",
    "activate_container",
    "check_skipped",
    "auto_inject",
    "factory_dependencies",
    "get_cache_stats",
    "print_cache_stats",
    "refresh",
    "skipped_during_discovery",
    "validate_autowiring",
]

# Free-threaded 3.14: a new thread must not inherit the library's context values (another
# request's scope). A no-op where threads do not inherit their starter's context.
from .container import _ThreadBirth as _thread_birth  # noqa: E402

_thread_birth._install()
