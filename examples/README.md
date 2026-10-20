# Deep layered examples

All four presentation technologies use the same deliberately deep application tree. They demonstrate that `auto_inject()` patches already-imported classes across nested folders and injects their runtime dependencies. The composition module imports the shared classes before autowiring; these examples do not demonstrate discovery of previously unimported modules under `examples/`, which is excluded from filesystem discovery.

```text
examples/
  shared/
    domain/contracts/
      messaging/message_repository.py
      formatting/message_formatter.py
      metadata/runtime_label_provider.py
    application/use_cases/greetings/
      greeting_service.py
    infrastructure/
      persistence/adapters/memory/repositories/message_repository.py
      presentation/formatting/turkish_message_formatter.py
      runtime/environment/metadata/providers/runtime_label_provider.py
    bootstrap/dependency_injection/
      composition.py
  console_app/main.py
  fastapi_app/app.py
  flask_app/app.py
  django_app/manage.py
```

The injected chain crosses all those layers:

```text
Presentation controller
  -> GreetingService
    -> MemoryMessageRepository
      -> TurkishMessageFormatter
        -> RuntimeLabelProvider
```

Every consumer declares dependencies only as `self.<name>_instance = None`. Application and presentation code contains no manual dependency-resolution calls. No `__init__.py` files are required for this namespace-package example.

## Run directly from IDLE or an editor

Open one of these entry-point files and choose **Run > Run Module (F5)** in IDLE:

- `console_app/main.py`: prints the complete chain's result in the IDLE shell.
- `fastapi_app/app.py`: starts at `http://127.0.0.1:8101/di-demo/World`.
- `flask_app/app.py`: starts at `http://127.0.0.1:8102/di-demo/Ayhan`.
- `django_app/manage.py`: starts at `http://127.0.0.1:8103/di-demo/Ayhan`.

No terminal command or manual `PYTHONPATH` setting is required. Install the optional framework dependencies from `examples/requirements.txt` once before running the web examples.

The main FastAPI endpoint constructs `FastApiController().execute(name)` directly;
its fields are populated automatically within the request scope. Optional
FastAPI-style `Depends(injected(...))` parameter injection is demonstrated at
`/di-demo-depends/World`.

## Visible proof in every output

Console prints every chain level line by line. FastAPI, Flask, and Django return the same five entries in the `injection_trace` JSON field. Each entry includes `module`, `module_depth`, `class`, `injected_field`, `injected_type`, `method`, `result`, and `instance_id`, so successful deep auto-injection is directly observable instead of inferred from one greeting.
