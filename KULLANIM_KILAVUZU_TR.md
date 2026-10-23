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
**Faydası:** `*_instance` alanlarının ulaştığı her şeyin bağlantı sorunları
açılışta, tek hatada raporlanır (bkz. *Bilinen sınırlar*).
**Kullanımı:**

```python
attach_container(app, container, validate=True)   # her köprü
auto_inject(container, validate=True)             # console
```

Eşleşen kaydı olmayan alanları, eşit derecede uygun adayları, çözülemeyen kurucu
bağımlılıklarını (okunamayan ve yeniden kurulamayan imza, çözülemeyen tür ipucu
ya da desteklenmeyen bir biçimde yazılmış `Keyed` dahil), taramanın import edemediği ya da yamalayamadığı proje modüllerini, bağımlılık döngülerini ve scoped bir servisi tutacak
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

İsteğe bağlı anahtarlı bağımlılık `Annotated[IClock, Keyed("utc")] = None`,
`Optional[Annotated[IClock, Keyed("utc")]] = None` (ya da `... | None`) veya
`Annotated[Optional[IClock], Keyed("utc")] = None` diye yazılır: anahtarlı servis
kayıtlıysa o, değilse varsayılan gelir. `Keyed` işaretinin başka bir yerde
olduğu biçimler (başka bir tiple birleşim, `list[...]` içi) desteklenmez; ama
asla sessizce yok sayılmaz - bir WARNING ve bir doğrulama sorunu alanı ve
anahtarı adıyla yazar.

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
sonra değiştirilen bir kayıt, ondan sonra oluşturulan tüketicilere ulaşır
(`refresh()` gerekmez). Değiştirilen türün alias'ları (`register_alias`) da yeni
kaydı izler.

Aynı test oturumunda birden fazla FastAPI uygulaması kullanıyorsanız her birini
`with` ile açın: açılışı kendi container'ını etkinleştirir. Başka bir uygulamanın
container'ı etkinken gelen istek, çözümü söyleyen açık bir hata alır; gerekirse
testler arasında `activate_container(container)` çağırın.

```python
def test_ilk():
    with TestClient(ilk_uygulama) as client:      # açılış ve kapanış çalışır
        client.get("/urunler")

def test_ikinci():
    with TestClient(ikinci_uygulama) as client:   # açılışı kendi container'ını etkinleştirir
        client.get("/siparisler")
```

### FastAPI parametre tarzı

**Neden:** bazı uç noktalarda FastAPI'nin `Depends` yapısını tercih ediyorsunuz.
**Kullanımı:** `controller=Depends(injected(ProductController))`; `injected`
`py_autowired.fastapi` içinden gelir. Nesnenin alanları aynı şekilde dolar.
Taramanın hiç yamalamadığı kayıtsız bir sınıf (proje kökü dışında ya da
açılıştan sonra oluşturulmuş) değiştirilmez: `injected()` kurduğu nesnenin
`None` kalan alanlarını `__init__` bittikten sonra doldurur; bu yüzden
`__init__`'in kendisi o alanları henüz kullanamaz. `__init__` alanlarına
ihtiyaç duyuyorsa sınıfı projeye taşıyın. Sınıfı yamalanmamış **kayıtlı** bir
türü ise `injected()` doldurmaz (paylaşılan bir singleton ilk isteğin
nesnelerini tutardı): `None` kalan her alan, adını veren bir WARNING ile `None`
kalır - sınıfı proje kökü içine alın ya da yapıcı enjeksiyonu kullanın.

### Log ve hata ayıklama çıktısı

Kütüphane `logging.getLogger("py_autowired")` üzerinden log yazar; her şey
yolundayken hiçbir şey yazmaz. Bir sorunu gizleyebilecek her durum WARNING (ya da
ERROR) olarak yazılır, hiçbiri yalnız DEBUG'da kalmaz - uygulama `logging`'i
yapılandırmışsa görünür (kütüphane kendi log çıkışını kurmaz):

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
kaybolmaz; sorunları loga yazılır ve denetlenir:

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

**Kökten çözüm uygulamadadır:** atlama, tarama uygulama modülü henüz import
edilirken başladığı için olur. Taramayı bu import bittikten sonra başlatırsanız
hiçbir dosya atlanmaz. Köprüler bunu zaten yapar (FastAPI'de `attach_container`
lifespan içinde, Flask ve Django ilk istekte). `auto_inject()`'i kendisi çağıran
bir açılış da aynısını yapabilir; o zaman her isteğe kendi scope'unu da verir ve
sonda container'ı kapatır (`attach_container`'ın sizin yerinize yaptığı):

```python
# app.py - attach_container() yerine auto_inject()'i kendisi çağırır
from contextlib import asynccontextmanager
from fastapi import FastAPI
from py_autowired import Container, auto_inject
from register import register

container = Container()
register(container)

@asynccontextmanager
async def lifespan(app):
    auto_inject(container)   # app.py import'u bitti: main.py normal yüklenir
    yield
    await container.aclose()

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def request_scope(request, call_next):
    async with container.create_scope():   # scoped servisler bir istek boyunca yaşar
        return await call_next(request)
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
  iki thread'i de adlandıran bir WARNING yazar; bunu hemen bir arka plan yazıcısı
  yazar (log işleyicisi bekleyen thread'i asla bloklamaz; uyarılar birikirse en
  eskiler düşer ve sayısı yazılır). Aynısı `Lazy` değeri (iki thread birbirinin
  `Lazy`'sini okursa `CircularDependencyError`), içinde oluşturma süren bir
  scope'un kapatılması ve kütüphanenin kendi kilidi için de geçerlidir
  (`auto_inject()` modül import ederken bekleyen `refresh()`,
  `activate_container()` ...). Testler GIL'siz CPython 3.14t'de de koşar.
- **Flask/Django köprülerinde kapanış:** container'ın oluşturduğu singleton'lar
  süreç çıkışında kapatılır; yalnız `aclose()` olanı kısa bir olay döngüsünde
  kapatılır.
- **Süreç başına bir container:** sınıf yamaları süreç geneline uygulanır; aynı
  süreçte birden çok uygulama desteklenmez - ayrı uygulamaları ayrı süreçlerde
  çalıştırın (gerçek destek 0.6.0'da, istek başına container ile). Container'ı
  etkin olmayan bir istek (FastAPI, Flask, Django) ya da `injected()` çağrısı
  hiçbir şey kurulmadan hata verir. Bu denetim en iyi çaba ile yapılan bir
  yakalamadır, garanti değildir. Art arda kullanılan uygulamalar (testler)
  sorunsuzdur: her açılış kendi container'ını etkinleştirir.
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

## Bilinen sınırlar (0.5.4)

0.5.4'ün söz vermedikleri, tek yerde:

- **Loglar ancak sizin logging ayarınızla görünür.** Kütüphane kendi log
  çıkışını kurmaz: `logging` yapılandırılmamışsa WARNING ve ERROR satırları
  gösterilmez.
- **Doğrulama, `*_instance` alanlarından ulaşılan grafiği kapsar.** Hiçbir
  alanın ulaşmadığı kayıtlı servis denetlenmez. Yanlış ya da boş `root_dir`,
  `validate=True` ile de WARNING'dir (servisleri yalnız elle kaydeden kod
  geçerlidir).
- **Birçok sorun önlenmez, görünür yapılır.** Bu durumlarda kütüphane WARNING
  yazıp devam eder: oluşturmakta olduğu servise ihtiyaç duyan bir worker'ı
  (`thread.join()`, `Event`) bekleyen factory ve `auto_inject()` sırasında
  kütüphaneyi kullanan başka bir thread'i bekleyen import donabilir (10 sn sonra
  iki thread'i adlandıran WARNING); eşit adaylarda ilk kayıt kullanılır; scoped
  servis çözen singleton onu saklayabilir; getter'ı hata veren property atlanır;
  kütüphane meşgulken oluşturulan sınıf alanlarını kütüphane boşalınca alır; tür
  ipuçları çözülemeyen, varsayılanı olan `Keyed` parametre varsayılanı alır.
  Hata olacakları "0.6.0'da gelecek" listeler.
- **Aynı süreçte birden çok uygulama: DESTEKLENMİYOR, 0.5.4'te çözülmedi.** Alan
  enjeksiyonu süreç geneli tek bir etkin container kullanır. İstek başında yapılan
  denetim en iyi çaba ile yapılan bir yakalamadır: istek sürerken etkinleştirilen
  bir container ve istek DIŞINDA (betik, arka plan thread'i) kurulan nesneler
  yakalanmaz - etkin container'ı kullanırlar. Kütüphanenin görmediği bir
  thread'de (bağlamı devretmeden başlattığınız) nesnenin hangi uygulamaya ait
  olduğunu süreç içinde hiçbir şey söylemez; bu orada hiç çözülemez. 0.5.5 birden
  çok canlı container için bir yönlendirici, 0.6.0 hiçbir bağlam olmadan kurulan
  nesne için açık hata getirecek. Hiç etkin container yokken (henüz hiçbir uygulama
  açılmamış, ör. yalnız `with` olmadan `TestClient`) alanlar 0.5.3'teki gibi
  doldurulmaz: her biri, adını veren bir WARNING ile `None` kalır. İkisini karıştırmak çalışmaz:
  `with` ile açılan bir uygulama durduktan sonra container'ı etkin kalır, bu
  yüzden `with` olmadan kullanılan başka bir uygulamanın her isteği hata verir -
  her uygulamayı `with` ile açın ya da önce `activate_container(container)`
  çağırın.
- **Kapanan bir scope** nesnelerini yalnız onu kapatan koda verir (sonlandırıcıları
  ve onların bağlam kopyasıyla başlattıkları), o da yalnız sonlandırıcısı henüz
  çalışmamış olanları; başka herkes `ScopeNotActiveError` alır. Scope kapanmadan
  ÖNCE elinize aldığınız nesne, tuttuğu şeyle çalışmaya devam eder - sonlandırıcısı
  kapattığı bir kaynağı kullanması engellenmez.
- **GIL'siz 3.14'te thread'ler:** yeni bir thread artık kütüphanenin bağlamını
  (başka bir isteğin scope'unu) miras almaz. Bu yüzden bir istek içinde başlatılan
  düz `threading.Thread`, 3.11'deki gibi isteğin scope'unu görmez; devretmek için
  `threading.Thread(..., context=contextvars.copy_context())` ya da
  `asyncio.to_thread` kullanın. Bunun için bir `sys.monitoring` araç kimliği
  (4; doluysa 3) kullanılır; tam bu kimlikleri isteyen bir araç onunla çakışır.
  Boş kimlik yoksa, import'taki denetimi başarısız olursa ya da kanca sonradan
  hata verirse kütüphane süreç sonuna kadar sert kurala geçer: scope yalnız ona
  giren thread'de okunur; başka her thread - `asyncio.to_thread` ve FastAPI'nin
  senkron uçlarının kullandığı anyio işçileri dahil - `ScopeNotActiveError` alır,
  başka bir isteğin nesnelerini asla. Import'taki düşüş bir WARNING yazar;
  sonradan olan düşüş log yazmaz (kanca bir kilit altında çalışıyor olabilir),
  nedeni her reddin `ScopeNotActiveError` mesajında yazar (`STATUS=hard-rule`).
  Durum `py_autowired._thread_birth.STATUS` ile okunur: `"active"`, `"hard-rule"`
  ya da `"not-needed"` (bu yapıda thread'ler miras almaz). Süreci yeniden
  başlatmadan geri dönüş yoktur. Aynı süreçte paketin ikinci bir kopyası
  yüklenirse (kütüphanenin kaynağı başka bir adla yeniden import edilirse) o
  kopya kendi container'ları için hep sert kuralı kullanır - ilk kopya import'ta
  düşmüş ve araç kimlikleri sonradan boşalmışsa hariç: o zaman yeni kopya kendi
  kancasını kurup kendisi için `"active"` olabilir (ilk kopya sert kuralda kalır).
- **Factory içinde kapatılan scope** (kendi scope'unu açıp kapatan bir factory)
  temizleyicilerini factory'nin oluşturma kilidi tutulurken çalıştırır.
  Kütüphanenin kendi kapanışı bunu asla yapmaz: kapatan thread o scope'taki bir
  oluşturmanın beklediği bir kilidi tutuyorsa hiçbir şey kapatılmaz, scope açık
  kalır ve `CircularDependencyError` verilir - kilit bırakıldıktan sonra yeniden
  kapatın.
- **Sınıfı yamalanmamış kayıtlı tür**, `None` alanlar için yalnız düz değerler
  okunarak denetlenir (kendi düz sözlüğü, düz sınıf değerleri, slotlar); nesnenin
  ya da sınıfın hiçbir kodu çalışmaz. Bu mümkün değilse (sınıf kendi `__dict__`'ini
  tanımlıyor, nesnenin `__dict__`'i bir `dict` alt sınıfı, bir sözlükte düz metin
  olmayan bir anahtar var ya da alana benzeyen bir
  ad property veya başka bir tanımlayıcı) sınıf başına bir WARNING, alanlarının
  denetlenemediğini söyler.
- **Anahtarında metin olmayan bir `__module__` ya da `__qualname__` bulunan tek
  seferlik WARNING** (kullanıcı kodu öyle atadıysa) bir kez değil her seferinde
  yazılır: kütüphane böyle bir nesneyi asla tutmaz ve hash'lemez.
- **Kütüphane kilidi altında çalışan kod:** keşif sırasında proje modüllerinin
  import'u ve kayıtlı türlerle anahtarların `__hash__`/`__eq__`'i kütüphane
  kilidi tutulurken çalışır. Bunlar, aynı container'dan çözen başka bir
  thread'i beklememeli (10 sn sonra iki thread'i de adlandıran bir WARNING).
  Kütüphanenin bıraktığı bir nesnenin (ör. eski isim haritasının kayıtları)
  `__del__`'i bir singleton oluşturulurken çalışabilir: `__del__` içinde
  container'dan çözmeyin, başka bir thread'i beklemeyin.
- **Proje dışındaki bir tabandan miras gelen alan** tabanın sınıf
  gövdesinden, `__slots__`'undan, ek açıklamalarından ve düz metotlarının `self`
  üzerine yaptığı atamalardan okunur (bir iç fonksiyonun yakaladığı `self` de).
  Statik ve sınıf metotlarında, hedef nesne ne olursa olsun her `*_instance`
  ataması sayılır (onlara `self` verilebilir): başka bir nesnenin alanı da
  bildirilebilir - görünür bir WARNING ya da doğrulama sorunu, asla sessiz `None`.
  Nesnesi adlandırılamayan bir atama (bir çağrının sonucu) sayılır - şüphede alan
  bildirilir, kaybolmaz; başka bir adla (`alias = self; alias.x_instance = ...`)
  ya da hesaplanan adla (`setattr`) yapılan atama görülmez. Böyle bir alan - ya da
  herhangi bir tabanın ek açıklaması - sınıfı "yalnız alan enjeksiyonu kullanan"
  sınıf yapmaz: kurucu bağımlılıkları eskisi gibi çözülür. Böyle bir tabanın kendine ait `*_instance` adlı özniteliği, adına
  uyan bir kayıt varsa doldurulur (bkz. CHANGELOG, "Behaviour changes"). Bir kez
  okunan taban yeniden okunmaz: sonradan ona eklenen metoda bakılmaz. Hangi
  atamanın `self` üzerine yapıldığı bayt kodundan okunur; bu okuma süreç başına
  bir kez bilinen şekillerle sınanır (3.10, 3.11, 3.14, 3.14t'de sınandı). Sınama
  tutmayan bir Python'da bir WARNING bunu söyler ve her atama sayılır (başka bir
  nesnenin alanı da bildirilebilir, hiçbiri kaybolmaz). Bir taban, dosyası
  süreçteki herhangi bir `auto_inject()` çağrısının kökü altındaysa proje içi
  sayılır: farklı köklerle yapılan çağrılardan sonra önceki bir kökün altındaki
  taban okunmaz (alanı, 0.5.4 öncesindeki gibi aday olmaz).
- **`injected()` hızı, 0.5.3'e göre** (CPython 3.11, Windows, bağımlılığın
  kendisinin tek çağrısı, ağ yok, sıcak önbellek): kayıtlı singleton yaklaşık
  +%10 (~110 ns), kayıtlı transient yaklaşık +%9 (~490 ns), kayıtsız denetleyici
  yaklaşık +%6 (~270 ns). Bilerek ödenen bedel: isteğin konteynerinin etkin
  konteyner olduğu denetimi (her bağımlılıkta da; WebSocket ya da ara katman
  dışı bir bağımlılık için tek denetim), kayıtlı türün alan denetimi (sınıfının
  yamalı olduğu bilindikten sonra tek kimlik karşılaştırması) ve ortak alan
  döngüsü.
- **Kütüphanenin istek başı maliyeti, 0.5.3'e göre** (CPython 3.11, Windows: istek
  ara katmanı - etkin konteyner denetimi, scope açma ve kapama - ve bir
  `injected()` bağımlılığı, web çatısı olmadan): boş scope yaklaşık 17,7 µs (0.5.3:
  12,6), kayıtlı singleton yaklaşık 19,6 µs (14,0), scoped servis yaklaşık 49,7 µs
  (24,8). Bilerek ödenen bedel: kütüphane kilidi tutulurken hiçbir log işleyicisi
  çalışmaz (scope kilidi de bunlardan biri), GIL'siz yapıda thread doğumu kuralı
  giren thread'i okur, kapanan scope sonlandırıcılarının bağlamını işaretler ve
  scoped servis kendi oluşturma kapısının arkasında kurulur - aynı scope'taki iki
  eşzamanlı istek asla iki nesne kurmaz, bir kilit döngüsü takılmak yerine hata
  verir. En büyük pay scoped oluşturma yolunun; yeniden tasarımı 0.5.5 için
  planlandı. Aynı makinede FastAPI üzerinden bir web isteği kendi başına yaklaşık
  150-230 µs ekler.
- **Log kaydının ne zaman yazıldığı:** bir iş parçacığı kütüphane kilidi
  tutarken (oluşturma kilidi, kütüphane kilidi, kayıt, sağlayıcı, scope, ad
  haritası, thread doğumu ya da başlangıç kilidi) kütüphanenin her kaydı (her
  düzey) bekletilir ve son kilidi bıraktığında yazılır - bir log işleyicisi kendi
  kilitlerini alabilir. Kilit almayan bir çözümleme (transient) hemen yazar. Bu
  yüzden arka plandaki bir iş parçacığının kaydı, o kilidi bıraktıktan bir an
  sonra görünebilir.
- **Kayıttan sonra değişen sınıf bilgisi** (örneğin kayıtlı sınıfın `__name__`'i)
  isim haritasınca kendiliğinden görülmez - `refresh()` çağırın.
- *(0.5.4'te çözüldü)* Eski changelog'lardaki iki sınır artık geçerli değil:
  `replace()` değiştirilen türün alias'larını da yeni kayda taşır; kalıtılan
  `__init__` içinde açıklamasız atanan alanlar enjekte edilir.

## 0.6.0'da gelecek

Bunlar çalışan kodun dayanabileceği davranışı değiştirir; bu yüzden 0.5.4'te
yalnız görünürdürler (WARNING ya da doğrulama sorunu), 0.6.0'da değişecekler:

- Eşit puanlı adaylar açılış hatası olacak; bugünkü davranışı isteyen kod için
  açık bir "ilk kayıt" seçimi olacak.
- Bir singleton'ın scoped servisi yakalaması hata olacak (singleton'dan scoped
  servis kullanmanın yolu `Provider`).
- Zorunlu ve isteğe bağlı `*_instance` alanları açıkça ayrılacak.
- Eksik tarama (import edilemeyen dosya, desteklenmeyen yükleyici), izin
  verilmedikçe açılışı durduracak.
- Container ile doğrulamanın ortak kullandığı tek bir oluşturma planı ve
  kullanıcı kodunu kütüphane kilidinin dışında import eden bir açılış.
- Getter'ı hata veren property'ler için açık bir kural.
- Tür ipuçları çözülemediğinde varsayılanı olan `Keyed` parametre, varsayılanı
  almak yerine hata verecek.
- Aynı süreçte birden çok uygulama: süreç başına tek etkin container yerine
  istek başına container (`ContextVar`).

0.6.0 için değerlendirilecek: yalnız alanların ulaştıklarını değil, bütün
kayıtları denetleyen isteğe bağlı bir doğrulama ve "bu alan neden bu servisi
aldı?" sorusunu yanıtlayan bir açıklama raporu. İş sırası: önce ortak oluşturma
planı ve kütüphane kilidinin dışında import eden açılış.

## Test kalitesi

Testler CPython 3.10, 3.11, 3.14 ve GIL'siz 3.14t üzerinde koşar. Enjeksiyon
motoru ve container cosmic-ray ile mutasyon testinden geçirildi: 1399 mutant;
davranışı değiştirebilenlerin **%99,3**'ü yakalandı (tümünün %88,0'ı; geri
kalanların eşdeğer olduğu gerekçeleriyle kanıtlandı). Ne yakalanan ne de eşdeğer
olduğu kanıtlanan 9 mutant açıkça listelendi. 0.5.1'de eklenen kod: 329 mutant,
davranışı değiştirebilenlerin **%99,0**'ı; 0.5.2'de değişen kod (bir sözleşmenin
birden çok uygulaması, büyük harfli alan adları): 216 mutant, davranışı
değiştirebilenlerin **%100**'ü yakalandı. 0.5.3 ve 0.5.4'te değişen kod elle
yazılmış mutantlarla sınandı: 322 mutant, **hepsi** yakalandı (her koşu önce
testleri mutantsız çalıştırır; geçmezlerse durur). Rapor:
[docs/MUTATION_REPORT.md](https://github.com/AyhanOztemel/py-autowired/blob/main/docs/MUTATION_REPORT.md).

## Lisans

MIT
