"""Public API for py-autowired."""

from importlib.metadata import PackageNotFoundError, version as _version

from .autowired import (
    AutoInjectionResolutionError,
    activate_container,
    auto_inject,
    get_cache_stats,
    print_cache_stats,
    refresh,
)
from .container import (
    AmbiguousImplementationError,
    AsyncDisposalRequiredError,
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
    "AutoInjectionResolutionError",
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
    "auto_inject",
    "factory_dependencies",
    "get_cache_stats",
    "print_cache_stats",
    "refresh",
]
