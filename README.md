# py-autowired

py-autowired injects runtime dependencies into fields declared with one rule:

```python
class UserService:
    def __init__(self):
        self.user_repository_instance = None
```

Application code contains no manual dependency-resolution calls. Register
the services once and call `auto_inject()` once during startup.

```python
from py_autowired.autowired import auto_inject
from py_autowired.container import Container

container = Container()
container.register_scoped(IUserRepository, SqliteUserRepository)
container.register_transient(UserService)
auto_inject(container, root_dir="/absolute/path/to/project")

with container.create_scope():
    service = UserService()
    print(service.user_repository_instance)
```

## Runtime package

The installed runtime contains only:

```text
py_autowired/
  autowired.py
  container.py
```

Python namespace packages make `__init__.py` unnecessary.

## Frameworks

The same core works with Console, FastAPI/ASGI, Flask/WSGI and Django. Framework
examples are under `examples/`; each uses domain, application, infrastructure,
composition and presentation layers without manual dependency resolution.

See [KULLANIM_KILAVUZU_TR.md](KULLANIM_KILAVUZU_TR.md) and
[USAGE_GUIDE_EN.md](USAGE_GUIDE_EN.md).
