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
Bir `register/` klasörü ihtiyaç duyulan sözleşmeleri listeler; her sözleşmenin
arkasındaki sınıfı py_autowired kendisi bulur.

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

**2. `register/register.py`** - sözleşmeleri ve her nesnenin ne kadar
yaşayacağını listeler. Hiçbir uygulama sınıfının adını yazmaz: her sözleşmeyi
uygulayan tek sınıfı py_autowired bulur:

```python
from domain.product_repository import IProductRepository
from domain.product_service import IProductService


def register(container):
    container.register_scoped(IProductRepository)    # bulur: MemoryProductRepository
    container.register_transient(IProductService)    # bulur: ProductService
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

**Alan adı nasıl yazılabilir?** Alan adındaki alt çizgi ve büyük/küçük harf de
fark etmez. Bu dört alanın dördü de `SqlProductRepository` alır:

```python
self.sql_product_repository_instance = None    # Python tarzı (önerilen)
self.sqlproductrepository_instance = None
self.sqlProductRepository_instance = None      # Java / C# tarzı
self.SqlProductRepository_instance = None
```

Yalnız son kısım sabittir: `_instance` tek nesne alır, `_instances` bir
sözleşmenin bütün uygulamalarını liste olarak alır (bkz. *Bir sözleşmenin
birden çok uygulaması*).

## Servis kaydı: `container.register_*`

| Çağrı | Yaşam süresi | Ne için |
| --- | --- | --- |
| `container.register_singleton(IClock)` | bütün uygulama için tek nesne | ayarlar, saat, paylaşılabilen istemciler |
| `container.register_scoped(IRepo, SqlRepo)` | scope başına bir nesne (bir web isteği) | veritabanı oturumu, iş birimi |
| `container.register_transient(IProductService)` | her seferinde yeni nesne | durumsuz servisler |
| `container.register_instance(Settings, settings)` | verdiğiniz nesne | kendi oluşturduğunuz nesneler; py_autowired onları kapatmaz |

- `register_*(Sözleşme)` - bütün örneklerde kullanılan biçim: py_autowired
  sözleşmenin tek uygulamasını kendisi bulur; uygulama başka bir katmanda olsa
  bile.
- `register_*(Sözleşme, Uygulama)` - bir sözleşmenin birden çok uygulaması
  varsa gerekir. Yazılmazsa py_autowired tahmin yürütmez,
  `AmbiguousImplementationError` hatasıyla durur. Birkaçını birden kullanmak
  için sözleşmeyi her sınıf için bir kez kaydedin (bkz. *Bir sözleşmenin
  birden çok uygulaması*).
- `register_*(SomutSınıf)` - sınıf kendi uygulamasıdır.
  `Container(strict_interfaces=True)` ile buna izin verilmez.
- **Yalnız sözleşme:** `Container(strict_interfaces=True)` (bütün örneklerde
  kullanılır) kayıt tipi olarak yalnız arayüz / abstract class kabul eder.
  Böylece projedeki her bağımlılık somut bir sınıfa değil, bir soyutlamaya
  bağlanır.
- `register_*` satırlarını `register/` içinde tutun. Böylece hiçbir dosya bir
  uygulama sınıfını import etmez: import'lar yalnız sözleşmelere gider.
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
bağımlılıklarını (okunamayan imza, çözülemeyen tür ipucu ya da yalnız konumsal
parametre dahil), taramanın import edemediği ya da yamalayamadığı proje modüllerini, bağımlılık döngülerini ve scoped bir servisi tutacak
singleton'ları listeler. Yalnız okur, hiçbir servis oluşturmaz. Sonradan tekrar
kontrol için: `validate_autowiring(container)` (`.issues` içeren
`AutowiringValidationError` fırlatır). Tarama sırasında yüklenemeyen bir dosya
ayrı bir satırda adıyla bildirilir ve yüklendiğinde denetlenir (bkz.
"Açılıştan sonra yüklenen dosyalar").

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
scope'tan çözülür. `Lazy[ScopedServis]` tutmamalıdır: `Lazy` ilk değeri saklar,
singleton da ilk isteğin nesnesini saklamış olur (`validate=True` bunu bildirir).
Oluşturulmakta olan bir `Lazy` yeniden okunursa donma yerine
`CircularDependencyError` verir.

### Bir sözleşmenin birden çok uygulaması

**Neden:** tek sözleşme, yan yana kullanılan birkaç sınıf - örneğin bellek ve
SQL repository'si ya da birkaç bildirim kanalı.
**Faydası:** .NET'teki (`IEnumerable<T>`) ve Spring'deki (`List<T>`) gibi bütün
uygulamalar kullanılabilir - yine `*_instance` alanlarıyla, import ve kurucu
parametresi olmadan.
**Nasıl:** sözleşmeyi her sınıf için bir kez kaydedin:

```python
# register/register.py
container.register_scoped(IProductRepository, MemoryProductRepository)
container.register_scoped(IProductRepository, SqlProductRepository)
```

```python
# application/report_service.py - hiçbir şey import etmez
class ReportService:
    def __init__(self):
        self.memory_product_repository_instance = None  # -> MemoryProductRepository
        self.sql_product_repository_instance = None     # -> SqlProductRepository
        self.product_repository_instance = None         # -> SqlProductRepository (son kaydedilen)
        self.product_repository_instances = None        # -> [MemoryProductRepository, SqlProductRepository]

    def count(self):
        return sum(len(repo.list_products()) for repo in self.product_repository_instances)
```

| Alan | Ne alır |
| --- | --- |
| sınıf adında (`sql_product_repository_instance`) | o sınıfı |
| sözleşme adında (`product_repository_instance`) | son kaydedilen sınıfı - eskisi gibi, `resolve()` ile aynı |
| sözleşme adı + `_instances` (`product_repository_instances`) | hepsini liste olarak, kayıt sırasıyla |

- Her kayıt kendi yaşam süresini korur (singleton, scoped ya da transient).
- Aynı sınıfı yeniden kaydetmek ikinci bir kopya eklemez; `replace()`
  sözleşmenin bütün önceki kayıtlarını siler.
- Alan kullanmayan kodda: `container.resolve_all(IProductRepository)` aynı
  listeyi döndürür.
- Sınıf yazmadan yalnız `register_scoped(IProductRepository)` yazılırsa ve iki
  uygulama varsa yine `AmbiguousImplementationError` ile durur: py_autowired
  hangisini kastettiğinizi tahmin etmez.
- `Container(strict_registrations=True)` bir sözleşmenin iki kez kaydedilmesini
  yasaklar (anahtarlı ve açık generic kayıtlar dahil); bu özellik orada
  kullanılamaz.

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
üzerinden kullanılır. Alanlarla *Bir sözleşmenin birden çok uygulaması*
yöntemini kullanın: orada anahtarın yerini sınıf adı tutar.

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

Kütüphane `logging.getLogger("py_autowired")` üzerinden log yazar; her şey
yolundayken hiçbir şey yazmaz. Bir sorunu gizleyebilecek her durum WARNING (ya da
ERROR) olarak yazılır, hiçbiri yalnız DEBUG'da kalmaz:

- import edilemeyen ya da keşfedilemeyen proje dosyası;
- eşleşen kaydı olmayan alan (alan başına bir kez; alan `None` kalır);
- uygulaması olmadan kaydedilmiş ve taramada uygulaması bulunamayan sözleşme;
- en iyi adayları eşit puanlı alanlar: açılışta TEK bir toplu WARNING; kayıt
  sırasındaki ilk aday kullanılır (`validate=True` bunları hata sayar). Aynı hazır
  nesnenin iki kaydı eşitlik değildir; bir factory'nin ne döndüreceği önceden
  bilinemez, bu yüzden aynı nesneyi döndüren iki factory yine eşitlik sayılır;
- çözülemeyen kurucu tür ipuçları (varsayılanı olan parametre varsayılanını alır);
- bir singleton oluşturulurken çözülen scoped servis (singleton onu saklarsa
  scope'undan uzun yaşar) - çift başına bir kez;
- import kancasının yamalayamadığı bir yükleyiciyle gelen proje modülü (`tests`
  gibi dışlanan klasörler hariç);
- yükleyecek ve yamalayacak hiçbir şey bulamayan `auto_inject()` (`root_dir`'i kontrol edin).

Ayrıntılı DEBUG çıktısı için import'tan önce `PY_AUTOWIRED_DEBUG=1` ayarlayın.

### Açılıştan sonra yüklenen dosyalar

Uygulama modülü henüz açılırken onu import eden bir proje dosyası (örneğin
`from app import app` yapan `main.py`) tarama sırasında yüklenemez. Bu dosya
kaybolmaz ve onunla ilgili hiçbir şey gizlenmez:

- açılışta INFO olarak yazılır: `Skipped main during discovery: ... it is checked when it loads`;
- dosya yüklendiği anda `*_instance` alanları doldurulur **ve denetlenir**
  (`validate=True` ile aynı denetim). Sorun hemen ERROR olur; `validate=True`
  ise o dosyanın import'u `AutowiringValidationError` ile durur. Sağlam dosya
  için INFO `Checked main after it loaded ...` yazılır;
- uygulama onu hiç import etmezse (sunucu doğrudan uygulama modülünden
  başlıyorsa) kütüphane onu ilk güvenli anda kendisi import eder: uygulama modülü
  yüklenmeyi bitirdikten sonraki ilk alan enjeksiyonu ya da `refresh()`,
  `activate_container()`, `validate_autowiring()`. Tarama zaten her proje
  dosyasını import eder; bu aynı import'un sonradan yapılmasıdır;
- `skipped_during_discovery()` atlanan adları verir; `check_skipped()` hepsini
  import edip denetler ve `validate` ne olursa olsun her sorunda
  `AutowiringValidationError` fırlatır:

```python
from py_autowired import check_skipped

def test_acilista_atlanan_dosyalar_baglanir():
    import app            # uygulama açılır
    check_skipped()       # sorun varsa hata fırlatır
```

Böyle bir dosya, kütüphane meşgulken başka bir thread'de yüklenmeyi bitirirse
artık o import durdurulamaz: sorun hemen ERROR olur ve `validate=True` ise sonraki
kütüphane çağrısı (`refresh()`, `activate_container()`, `auto_inject()`,
`validate_autowiring()` ya da sonraki enjeksiyon) kendi işini bitirdikten sonra
onu fırlatır. Henüz yüklenmemiş atlanmış dosyayı `validate_autowiring()` ayrı
bir satırda adıyla bildirir.

## Notlar

- **Eşzamanlılık:** hazır singleton'lar ve önbellek okumaları kilitsizdir. Bir
  bağımlılık döngüsü, iki thread aynı anda çarpsa bile donma yerine
  `CircularDependencyError` verir. Her scoped servis kendi kapısı altında
  oluşturulur; bir factory, aynı scope'un başka bir scoped servisine ihtiyaç
  duyan bir worker thread'i bekleyebilir. Bilinen sınır: factory, tam da
  oluşturmakta olduğu servise ihtiyaç duyan bir worker'ı (ör. `thread.join()` ile)
  beklerse sonsuza dek bekler - bu beklemeyi hiçbir kilit göstermez; singleton'da
  da scoped'da da aynıdır. Sessiz değildir: bir oluşturma kapısında
  `container.creation_wait_warning` saniyeden (varsayılan 10) uzun bekleyen thread
  iki thread'i de adlandıran bir WARNING yazar - kendisi oluşturma kilidi
  tutmuyorsa hemen, tutuyorsa onları bıraktığında. Testler GIL'siz CPython
  3.14t'de de koşar.
- **Flask/Django köprülerinde kapanış:** container'ın oluşturduğu singleton'lar
  süreç çıkışında kapatılır; yalnız `aclose()` olanı kısa bir olay döngüsünde
  kapatılır.
- **Süreç başına bir container:** sınıf yamaları süreç geneline uygulanır; ayrı
  uygulamaları ayrı süreçlerde çalıştırın.
- **Tarama modülleri import eder:** proje klasörü altındaki modüller import
  edilir. Import edilmemesi gereken klasörler için `exclude_dirs` kullanın. Proje
  içinde tutulan bir kütüphane kopyası (vendor) ya da ikinci bir kurulum adıyla
  dışlanmalıdır: `auto_inject(container, exclude_dirs=["vendor", "third_party"])`.
- **Hangi ad alan sayılır:** `_instance`/`_instances` ile biten ve değer
  alabilen ad - sınıf değişkeni, açıklama (annotation), `self.x_instance = ...`
  ataması, `__slots__`/attrs/dataclass alanı ya da ayarlayıcısı (setter) olan
  property. Bu adla biten bir metot ya da salt-okunur property (`has_instance`)
  alan değildir.
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
olduğu kanıtlanan 9 mutant açıkça listelendi. 0.5.1'de eklenen kod: 329 mutant,
davranışı değiştirebilenlerin **%99,0**'ı; 0.5.2'de değişen kod (bir sözleşmenin
birden çok uygulaması, büyük harfli alan adları): 216 mutant, davranışı
değiştirebilenlerin **%100**'ü yakalandı. Rapor:
[docs/MUTATION_REPORT.md](https://github.com/AyhanOztemel/py-autowired/blob/main/docs/MUTATION_REPORT.md).

## Lisans

MIT
