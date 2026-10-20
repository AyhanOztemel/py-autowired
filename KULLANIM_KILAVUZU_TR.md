# py_autowired kullanım kılavuzu

**Python için çalışma zamanı bağımlılık enjeksiyonu; sınıflarınız birbirini import etmez.**

Bir sınıf yalnız neye ihtiyacı olduğunu yazar:

```python
class ProductService:
    def __init__(self):
        self.product_repository_instance = None   # kendiliğinden dolar
```

py_autowired `product_repository_instance` alanını çalışma anında doldurur.
`ProductService` repository'yi import etmez, onu kurucu parametresi olarak
almaz ve hiçbir container çağrısı yapmaz. Sınıflar birbirinden bağımsız kalır.
Onları import eden tek yer bir `register/` klasörüdür.

English: [README.md](README.md)

## Kurulum

```bash
pip install py_autowired
```

## Hızlı başlangıç (FastAPI)

**1. Sınıflarınız** - sözleşmelere (arayüz / abstract class) bağlıdır ve
birbirini import etmez; bir uygulama yalnız uyguladığı sözleşmeyi import eder:

```python
# application/product_service.py
from domain.product_service import IProductService

class ProductService(IProductService):
    def __init__(self):
        self.product_repository_instance = None   # -> IProductRepository

    def list_products(self):
        return self.product_repository_instance.list_products()

# presentation/product_controller.py
class ProductController:
    def __init__(self):
        self.product_service_instance = None      # -> IProductService

    def list_products(self):
        return self.product_service_instance.list_products()
```

**2. `register/register.py`** - sınıfları import eden tek dosya; her ihtiyaç
için hangi sınıfın kullanılacağını ve ne kadar yaşayacağını söyler:

```python
from application.product_service import ProductService
from domain.product_repository import IProductRepository
from domain.product_service import IProductService
from infrastructure.memory_product_repository import MemoryProductRepository


def register(container):
    container.register_scoped(IProductRepository, MemoryProductRepository)
    container.register_transient(IProductService, ProductService)
```

**3. `bootstrap/bootstrap.py`** - uygulama nasıl ayağa kalkar:

```python
from py_autowired import Container
from py_autowired.fastapi import attach_container

from register import register


def bootstrap(app):
    container = Container(strict_interfaces=True)   # yalnız sözleşmeler kaydedilebilir
    register(container)
    attach_container(app, container)
```

**4. `main.py`** - tek çağrı:

```python
from fastapi import FastAPI
from bootstrap import bootstrap
from presentation.product_controller import ProductController

app = FastAPI()
bootstrap(app)

@app.get("/products")
def products():
    return ProductController().list_products()   # alanlar zaten dolu
```

`uvicorn main:app` ya da `python main.py` ile çalıştırın. `root_dir`, lifespan
ya da middleware yazmanız gerekmez: proje FastAPI açılırken (lifespan içinde)
taranır, her istek kendi scope'unu alır, FastAPI kapanırken singleton'lar
kapatılır. Projenin tamamı: [examples/fastapi](examples/fastapi).

## Üç kural

1. **Bildir, import etme.** Adı `_instance` ile biten ve `None` atanmış alan bir
   bağımlılıktır. Adı sözleşmeyi seçer: `product_service_instance` demek
   `IProductService` demektir (bkz. *Adlandırma*). Sizin atadığınız bir değerin
   üzerine yazılmaz; eşleşen kaydı olmayan alan `None` kalır.
2. **Tek yerde kaydet.** `register/` sınıfları import eder; her ihtiyaç için
   hangi sınıfın oluşturulacağını ve ne kadar yaşayacağını söyler.
3. **Bir kez ayağa kaldır.** `bootstrap/` container'ı oluşturur, kayıtları ekler
   ve framework'ü tek köprü çağrısıyla bağlar (bkz. *Framework'ler*); giriş
   dosyası `bootstrap(...)`'ı bir kez çağırır.

Tip açıklaması isteğe bağlıdır
(`product_service_instance: IProductService | None = None` de çalışır). Editörünüze
yardım eder ama import gerektirir; py_autowired için ad tek başına yeterlidir.

## Adlandırma: hangi alana hangi sınıf gelir

Alanın adı (`_instance` olmadan) kayıtlı sözleşme ve sınıf adlarıyla
karşılaştırılır. Alt çizgi ve büyük/küçük harf fark etmez; şu sözleşme önekleri
(ve `Impl` soneki) tanınır:

| Sözleşme adı | Onu alan alan |
| --- | --- |
| `IProductService`, `I_ProductService` | `product_service_instance` |
| `ABSProductService`, `ABS_ProductService` | `product_service_instance` |
| `AbsProductService`, `Abs_ProductService` | `product_service_instance` |
| `absProductService`, `abs_ProductService` | `product_service_instance` |
| `ProductServiceImpl` | `product_service_instance` |
| `ProductService` (sözleşmesiz) | `product_service_instance` |

Önek yalnız ardından büyük harf (ya da alt çizgi) gelirse sayılır; böylece
`Absolute` ya da `Ignore...` gibi adlar sıradan kelime olarak kalır.

## Servis kaydı: `container.register_*`

| Çağrı | Yaşam süresi | Ne için |
| --- | --- | --- |
| `container.register_singleton(IClock, SystemClock)` | bütün uygulama için tek nesne | ayarlar, saat, paylaşılabilen istemciler |
| `container.register_scoped(IRepo, SqlRepo)` | scope başına bir nesne (bir web isteği) | veritabanı oturumu, iş birimi |
| `container.register_transient(IProductService, ProductService)` | her seferinde yeni nesne | durumsuz servisler |
| `container.register_instance(Settings, settings)` | verdiğiniz nesne | kendi oluşturduğunuz nesneler; py_autowired onları kapatmaz |

- `register_*(Sözleşme, Uygulama)` - en açık biçim.
- `register_*(Sözleşme)` - py_autowired sözleşmenin tek uygulamasını kendisi
  bulur; uygulama başka bir katmanda olsa bile.
- `register_*(SomutSınıf)` - sınıf kendi uygulamasıdır.
  `Container(strict_interfaces=True)` ile buna izin verilmez.
- **Yalnız sözleşme:** `Container(strict_interfaces=True)` (bütün örneklerde
  kullanılır) kayıt tipi olarak yalnız arayüz / abstract class kabul eder.
  Böylece projedeki her bağımlılık somut bir sınıfa değil, bir soyutlamaya
  bağlanır.
- `register_*` satırlarını uygulama sınıflarını import eden tek yer olan
  `register/` içinde tutun.
- Bir sınıfı, **başka bir sınıf ona ihtiyaç duyduğunda** kaydedin. Yalnız
  bağımlılık alan bir sınıf (controller) normal oluşturulur: `ProductController()`.
- `close()`/`dispose()` (ya da `aclose()`) metodu olan scoped nesneler scope
  bitince kapatılır. Container'ın oluşturduğu singleton'lar uygulama kapanınca
  (web köprüleri) ya da siz `container.close()` çağırınca kapatılır.

## Framework'ler

Her örnek, aynı katmanlara ve kendi README'sine sahip eksiksiz bir projedir.

| Framework | Köprü çağrısı (`bootstrap/` içinde) | Scope | Örnek |
| --- | --- | --- | --- |
| Console | `auto_inject(container)` | `with container.create_scope():` | [examples/console](examples/console) |
| FastAPI | `attach_container(app, container)` | her HTTP isteği | [examples/fastapi](examples/fastapi) |
| Flask | `attach_container(app, container)` | her HTTP isteği | [examples/flask](examples/flask) |
| Django | `application = attach_container(application, container)` | her HTTP isteği | [examples/django](examples/django) |

Köprüyü `py_autowired.fastapi`, `py_autowired.flask` ya da `py_autowired.django`
modülünden import edin. Her köprü proje klasörünü kendisi bulur
(`requirements.txt`, `pyproject.toml`, `.git`, `main.py`/`app.py`; Django'da
`manage.py` olan klasör), onu bir kez tarar, her isteğe bir scope açar ve
uygulama kapanırken singleton'ları kapatır.

## İsteğe bağlı özellikler

Yukarıdaki kurulum için bunların hiçbiri gerekmez. Her biri belirli bir sorunu
çözer; yalnız o sorun varsa kullanın.

### Açılışta doğrulama

**Neden:** eksik bir kayıt normalde ancak alan kullanılınca ortaya çıkar.
**Faydası:** bütün bağlantı sorunları açılışta, tek hatada raporlanır.
**Kullanımı:**

```python
attach_container(app, container, validate=True)   # her köprü
auto_inject(container, validate=True)             # console
```

Eşleşen kaydı olmayan alanları, eşit derecede uygun adayları, çözülemeyen kurucu
bağımlılıklarını, bağımlılık döngülerini ve scoped bir servisi tutacak
singleton'ları listeler. Yalnız okur, hiçbir servis oluşturmaz. Sonradan tekrar
kontrol için: `validate_autowiring(container)` (`.issues` içeren
`AutowiringValidationError` fırlatır).

### `Provider[T]` ve `Lazy[T]`

**Neden:** bazen bir nesne baştan oluşturulmamalıdır; pahalıdır ya da bir isteğe
aittir ama onu tutan nesne daha uzun yaşar.
**Faydası:** nesneyi gerektiği anda oluşturmak.
**Kullanımı** (kurucu parametreleri):

```python
from py_autowired import Lazy, Provider

class ReportService:
    def __init__(self, sessions: Provider[Session], exporter: Lazy[PdfExporter]):
        self.sessions = sessions    # self.sessions() -> o anki scope'un Session'ı
        self.exporter = exporter    # self.exporter.value -> ilk kullanımda oluşur, sonra aynısı
```

Bir singleton `Provider[ScopedServis]` tutabilir: her çağrı o anda etkin olan
scope'tan çözülür.

### Birden çok uygulama arasında seçim: `set_primary`

**Neden:** kayıtlı iki sınıf aynı alan adıyla eşleşiyor.
**Faydası:** alan adlarını değiştirmeden kazananı bir kez belirlemek.
**Kullanımı:** `container.set_primary(UtcClock)` (Spring'deki `@Primary` gibi).
Yoksa tüketicinin modülüne en yakın sınıf kazanır.

### Anahtarlı servisler

**Neden:** bir arayüzün birlikte kullanılan birkaç uygulaması var
(örneğin UTC ve yerel saat).
**Faydası:** uygulamayı bir anahtarla seçmek.
**Kullanımı:**

```python
from typing import Annotated
from py_autowired import Keyed

container.register_keyed(IClock, "utc", UtcClock)
container.register_keyed(IClock, "local", LocalClock)

class Invoice:
    def __init__(self, clock: Annotated[IClock, Keyed("utc")]): ...
```

Anahtarlı kayıtlar `*_instance` alanlarıyla değil, kurucu ve `resolve`
üzerinden kullanılır.

### Açık generic kayıt

**Neden:** birçok tipin aynı türde servise ihtiyacı var (`Repository[User]`,
`Repository[Order]`, ...).
**Faydası:** tek kayıt hepsine hizmet eder.
**Kullanımı:**

```python
container.register_open_generic(Repository, SqlRepository)   # class SqlRepository(Repository[T])
```

Kapalı generic'ler `*_instance` alanlarıyla değil, kurucu ve `resolve`
üzerinden kullanılır.

### Container'ı kendiniz kapatmak

**Neden:** köprüsü olmayan bir konsol programı ya da betik kaynaklarını
bırakmalı.
**Faydası:** singleton'lar oluşturuldukları sıranın tersiyle kapatılır.
**Kullanımı:** iş bitince `container.close()` ya da `await container.aclose()`.
Framework köprüleri bunu uygulama kapanırken zaten yapar. `register_instance`
ile verilen nesneler hiçbir zaman kapatılmaz. `close()` sonrasında `resolve()`
`ContainerClosedError` fırlatır.

### Testlerde servis değiştirmek

**Neden:** test gerçek veritabanı yerine sahte bir sınıf kullanmalı.
**Faydası:** test edilen kod değişmez.
**Kullanımı:**

```python
container.replace(IProductRepository, FakeProductRepository)
```

Her test için yeni bir container kullanın. Otomatik enjeksiyon başladıktan
sonra bir kaydı değiştirirseniz yeni tüketiciler oluşturmadan önce
`refresh()` çağırın.

### FastAPI parametre tarzı

**Neden:** bazı uç noktalarda FastAPI'nin `Depends` yapısını tercih ediyorsunuz.
**Kullanımı:** `controller=Depends(injected(ProductController))`; `injected`
`py_autowired.fastapi` içinden gelir. Nesnenin alanları aynı şekilde dolar.

### Log ve hata ayıklama çıktısı

Kütüphane `logging.getLogger("py_autowired")` üzerinden log yazar ve varsayılan
olarak sessizdir. Tarama sorunları WARNING olarak loglanır. Ayrıntılı DEBUG
çıktısı için import'tan önce `PY_AUTOWIRED_DEBUG=1` ayarlayın.

## Notlar

- **Eşzamanlılık:** hazır singleton'lar ve önbellek okumaları kilitsizdir. Bir
  bağımlılık döngüsü, iki thread aynı anda çarpsa bile donma yerine
  `CircularDependencyError` verir. Testler GIL'siz CPython 3.14t'de de koşar.
- **Süreç başına bir container:** sınıf yamaları süreç geneline uygulanır; ayrı
  uygulamaları ayrı süreçlerde çalıştırın.
- **Tarama modülleri import eder:** proje klasörü altındaki modüller import
  edilir. Import edilmemesi gereken klasörler için `exclude_dirs` kullanın.
  Proje içindeki `tests` ve `examples` klasörleri kendiliğinden atlanır.
- **`resolve()` senkrondur:** ham coroutine döndüren fabrika
  `AsyncFactoryError` verir.
- **Uyumluluk:** sıradan sınıflar, `__slots__`, değiştirilebilir dataclass'lar ve
  değiştirilebilir Pydantic v2 modelleri (`arbitrary_types_allowed=True` ile)
  çalışır. Dondurulmuş dataclass ve Pydantic modellerine alan atanamaz (nedenini
  `AutoInjectionResolutionError` açıklar).
- **Hız:** sıcak önbellekte enjekte edilen bir alan, nesne oluşturma başına
  birkaç mikrosaniye ekler; kendi makinenizde ölçmek için
  `python -B benchmarks/field_injection.py --number 100000 --repeat 5`.

## Test kalitesi

Testler CPython 3.10, 3.11, 3.14 ve GIL'siz 3.14t üzerinde koşar. Enjeksiyon
motoru ve container cosmic-ray ile mutasyon testinden geçirildi: 1399 mutant;
davranışı değiştirebilenlerin **%99,3**'ü yakalandı (tümünün %88,0'ı; geri
kalanların eşdeğer olduğu gerekçeleriyle kanıtlandı). Ne yakalanan ne de eşdeğer
olduğu kanıtlanan 9 mutant açıkça listelendi. Rapor:
[docs/MUTATION_REPORT.md](https://github.com/AyhanOztemel/py-autowired/blob/main/docs/MUTATION_REPORT.md).

## Lisans

MIT
