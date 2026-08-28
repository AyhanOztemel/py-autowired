from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SOURCE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from py_autowired.autowired import auto_inject
from py_autowired.container import Container
from examples.shared.application.use_cases.greetings.greeting_service import GreetingService
from examples.shared.domain.contracts.formatting.message_formatter import IMessageFormatter
from examples.shared.domain.contracts.messaging.message_repository import IMessageRepository
from examples.shared.domain.contracts.metadata.runtime_label_provider import IRuntimeLabelProvider
from examples.shared.infrastructure.persistence.adapters.memory.repositories.message_repository import MemoryMessageRepository
from examples.shared.infrastructure.presentation.formatting.turkish_message_formatter import TurkishMessageFormatter
from examples.shared.infrastructure.runtime.environment.metadata.providers.runtime_label_provider import RuntimeLabelProvider


container = Container()
_configured = False


def configure_dependencies() -> Container:
    global _configured
    if not _configured:
        container.register_singleton(IRuntimeLabelProvider, RuntimeLabelProvider)
        container.register_transient(IMessageFormatter, TurkishMessageFormatter)
        container.register_scoped(IMessageRepository, MemoryMessageRepository)
        container.register_transient(GreetingService)
        auto_inject(container, root_dir=str(PROJECT_ROOT))
        _configured = True
    return container
