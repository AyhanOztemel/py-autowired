# Console ile py_autowired

Küçük ama eksiksiz bir Console projesi. Önce çalıştırın, sonra aşağıdaki
klasör listesinin sırasıyla dosyaları okuyun.

## Çalıştırma

```bash
pip install -r requirements.txt
python main.py
```

## Üç kuralda fikir

1. **Sınıflar birbirini import etmez.** Bir sınıf yalnız neye ihtiyacı
   olduğunu yazar, örneğin `self.product_service_instance = None`. Alanın adı
   hangi sözleşmenin kastedildiğini söyler: `product_service_instance` bir
   `IProductService`, `product_repository_instance` bir `IProductRepository`.
   Alanlar çalışma anında kendiliğinden dolar.
2. **`register/` yalnız sözleşmeleri yazar.** Uygulamanın kullandığı
   sözleşmeler ve her nesnenin ne kadar yaşayacağı burada yazılır. Her
   sözleşmeyi uygulayan sınıfı py_autowired kendisi bulur; hiçbir uygulama
   sınıfı hiçbir yerde import edilmez.
3. **`bootstrap/` uygulamayı ayağa kaldırır:** container'ı oluşturur, kayıtları
   ekler ve Console'a bağlar. Giriş dosyanız onu bir kez çağırır:
   `container = bootstrap()`.

## Klasörler

```text
domain/                 # yalnız sözleşmeler (arayüzler), kod yok
  product_repository.py # IProductRepository
  clock.py              # IClock
  product_service.py    # IProductService
infrastructure/         # sözleşmelerin uygulamaları
  memory_product_repository.py
  system_clock.py
application/            # iş mantığı
  product_service.py    # IProductService'i uygular; repository ve saate ihtiyaç duyar
presentation/           # kullanıcının çağırdığı yer
  product_controller.py # IProductService'e ihtiyaç duyar - hiçbir şey import etmez
register/               # hangi sözleşmeler kullanılır - uygulama sınıfı yazmaz
  register.py           # sözleşme + ne kadar yaşar
bootstrap/              # uygulama nasıl ayağa kalkar
  bootstrap.py          # container + kayıtlar + Console köprüsü
main.py                 # programı başlatır
requirements.txt        # proje kökünü de belirtir
```

Katmanlar arasındaki import'lar yalnız `domain/`'e gider: bir uygulama sınıfı
uyguladığı sözleşmeyi import eder (`MemoryProductRepository(IProductRepository)`,
`ProductService(IProductService)`), `register/` de sözleşmeleri import eder.
`infrastructure/` ya da `application/` klasörünü kimse import etmez.

## register/register.py

```python
from domain.clock import IClock
from domain.product_repository import IProductRepository
from domain.product_service import IProductService


def register(container):
    container.register_singleton(IClock)
    container.register_scoped(IProductRepository)
    container.register_transient(IProductService)
```

## bootstrap/bootstrap.py

```python
from py_autowired import Container, auto_inject

from register import register


def bootstrap() -> Container:
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    auto_inject(container)        # scans the project; *_instance fields are filled from now on
    return container
```

| Yaşam süresi | Kaydedilen sözleşme → kendiliğinden bulunan sınıf | Anlamı |
| --- | --- | --- |
| `register_singleton` | `IClock` → `SystemClock` | bütün uygulama için tek nesne |
| `register_scoped` | `IProductRepository` → `MemoryProductRepository` | iş birimi (`main.py` içindeki `with container.create_scope():`) başına bir nesne; sonunda `close()` çağrılır |
| `register_transient` | `IProductService` → `ProductService` | her ihtiyaçta yeni bir nesne |

**Birden çok uygulama mı var?** Bir sözleşmenin iki uygulaması varsa (örneğin
`MemoryProductRepository` ve `SqlProductRepository`) yalnız sözleşmeyi yazmak
yetmez: py_autowired tahmin yürütmez, `AmbiguousImplementationError` hatasıyla
durur. Kullanmak istediğiniz her sınıfı kaydedin:

```python
container.register_scoped(IProductRepository, MemoryProductRepository)
container.register_scoped(IProductRepository, SqlProductRepository)
```

O zaman `memory_product_repository_instance` ve `sql_product_repository_instance`
kendi sınıfını, `product_repository_instance` son kaydedileni,
`product_repository_instances` ise ikisini birden liste olarak alır. Ayrıntı:
ana kılavuzdaki *Bir sözleşmenin birden çok uygulaması* başlığı.

`ProductController` kaydedilmez: yalnız bağımlılık *alır*, onu siz
`ProductController()` diye oluşturursunuz. Başka bir sınıfın ihtiyaç duyduğu
sınıfları kaydedin.

## Adlandırma: hangi alana hangi sınıf gelir

Alanın adı (`_instance` olmadan) kayıtlı sözleşme ve sınıf adlarıyla
karşılaştırılır. Alt çizgi ve büyük/küçük harf fark etmez; şu sözleşme önekleri
(ve `Impl` soneki) tanınır:

| Sözleşme adı | Onu alan alan |
| --- | --- |
| `IProductRepository`, `I_ProductRepository` | `product_repository_instance` |
| `ABSProductRepository`, `ABS_ProductRepository` | `product_repository_instance` |
| `AbsProductRepository`, `Abs_ProductRepository` | `product_repository_instance` |
| `absProductRepository`, `abs_ProductRepository` | `product_repository_instance` |
| `ProductRepositoryImpl` | `product_repository_instance` |

Alanın kendisi şu dört biçimden biriyle yazılabilir; dördü de aynı nesneyi
alır: `product_repository_instance`, `productrepository_instance`,
`productRepository_instance`, `ProductRepository_instance`. Yalnız son kısım
sabittir: `_instance` (tek nesne) ya da `_instances` (bütün uygulamalar, liste).

## Yalnız sözleşme: `strict_interfaces=True`

`bootstrap/` içindeki container `Container(strict_interfaces=True)` ile
oluşturulur: yalnız bir sözleşme (arayüz / abstract class) kaydedilebilir,
somut sınıf kaydedilemez. Böylece projedeki her bağımlılık bir soyutlamaya
bağlanır.

## Köprü

Konsol programında HTTP isteği yoktur, bu yüzden web köprüsü de yoktur: `auto_inject(container)` enjeksiyonu açar, `main.py` de bir iş birimi için tek bir scope açar (`with container.create_scope():`).

## Kendi projenizde

1. Sınıflarınızı `*_instance = None` alanlarıyla yazın; birbirine import etmeyin.
2. `register/` ve `bootstrap/` klasörlerini kopyalayın; `register/register.py`
   içinde kendi sözleşmelerinizi import edip her sözleşme için bir
   `register_*` satırı yazın.
3. `main.py` dosyasındaki gibi `bootstrap(...)`'ı bir kez çağırın.


İsteğe bağlı özellikler (açılışta doğrulama, `Provider`/`Lazy`, anahtarlı
servisler, generic'ler, ...) ana README'de *İsteğe bağlı özellikler*
başlığında anlatılır.
