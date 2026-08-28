# py-autowired Türkçe Kullanım Kılavuzu

## Temel kural

Runtime bağımlılığı constructor parametresi veya manuel container çağrısı değildir:

```python
class SiparisServisi:
    def __init__(self):
        self.siparis_repository_instance = None
```

`auto_inject()`, alan adındaki `siparis_repository` bölümünü kayıtlı
`ISiparisRepository` veya `SiparisRepository` tipiyle eşleştirir ve nesneyi
çalışma anında yerleştirir.

## Kurulum

```bash
pip install py-autowired
```

## Kayıt ve başlangıç

```python
from py_autowired.autowired import auto_inject
from py_autowired.container import Container

container = Container()
container.register_singleton(IClock, SystemClock)
container.register_scoped(IUserRepository, SqliteUserRepository)
container.register_transient(UserService)

# Uygulama başlangıcında yalnız bir kez:
auto_inject(container, root_dir="/uygulamanin/mutlak/yolu")
```

`auto_inject()` çağrısından önce ilgili soyut ve somut sınıflar import edilmiş
ve kayıtlar tamamlanmış olmalıdır. Enjekte edilecek uygulama nesnelerini bu
çağrıdan sonra oluşturun.

## Yaşam süreleri

- `register_singleton`: Uygulama süresince aynı nesne.
- `register_scoped`: Bir scope içinde aynı, sonraki scope'ta yeni nesne.
- `register_transient`: Her enjeksiyonda yeni nesne.
- `register_instance`: Önceden oluşturulmuş nesneyi kullanır.
- `register_factory`: Parametresiz factory ile nesne oluşturur.

Console, Flask ve Django gibi senkron yollarda:

```python
with container.create_scope():
    controller = UserController()
    result = controller.execute()
```

FastAPI gibi asenkron yollarda:

```python
async with container.create_scope():
    controller = UserController()
    result = await controller.execute()
```

## Katmanlı mimari

- Domain: soyut repository/port.
- Infrastructure: somut repository/adaptör.
- Application: `self.repository_instance = None` kullanan servis.
- Composition: kayıtlar ve tek `auto_inject()` çağrısı.
- Presentation: Console, FastAPI, Flask veya Django controller/view.

Sunum ve uygulama katmanlarında manuel bağımlılık çözümleme çağrısı kullanılmaz. Ayrıntılı, çalışan örnekler `examples/` klasöründedir.

## İsim eşleştirme

Alan mutlaka küçük harfle başlamalı ve `_instance` ile bitmelidir:

- `user_repository_instance` → `IUserRepository`
- `payment_service_instance` → `PaymentService`
- `audit_instance` → `ABS_Audit`

Bir ad birden fazla kayda uyuyorsa modül yakınlığı kullanılır. Belirsiz kayıtları
önlemek için servis adlarını açık ve benzersiz tutun.

## Kırmızı çizgi

Tek geçerli bağımlılık bildirimi: self.repository_instance = None.

## IDLE ile tek tuşla çalıştırma

Terminal komutu yazmanız gerekmez. IDLE içinde aşağıdaki ana dosyalardan birini açın ve **Run > Run Module (F5)** seçin:

- Console: `examples/console_app/main.py`
- FastAPI: `examples/fastapi_app/app.py` — `http://127.0.0.1:8101/di-demo/Ayhan`
- Flask: `examples/flask_app/app.py` — `http://127.0.0.1:8102/di-demo/Ayhan`
- Django: `examples/django_app/manage.py` — `http://127.0.0.1:8103/di-demo/Ayhan`

Web örnekleri için `examples/requirements.txt` içindeki isteğe bağlı framework bağımlılıklarının bir kez kurulmuş olması gerekir. Kaynak klasöründen çalıştırırken ayrıca `PYTHONPATH` ayarlamanız gerekmez.

## Derin klasör kanıtı

`examples/shared` bilinçli olarak düz tutulmamıştır. Domain sözleşmeleri, use-case, repository, formatter, runtime sağlayıcı ve composition root farklı ve iç içe klasörlerdedir. Çalışan enjeksiyon zinciri şöyledir:

`Controller -> GreetingService -> MemoryMessageRepository -> TurkishMessageFormatter -> RuntimeLabelProvider`

Bu zincirdeki her bağımlılık yalnız `self.<ad>_instance = None` bildirimiyle yerleştirilir. Böylece örnekler, klasör ve modül derinliği arttığında da `auto_inject()` mekanizmasının çalıştığını doğrular.
