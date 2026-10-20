# Django ile py_autowired

Küçük ama eksiksiz bir Django projesi. Önce çalıştırın, sonra aşağıdaki
klasör listesinin sırasıyla dosyaları okuyun.

## Çalıştırma

```bash
pip install -r requirements.txt
python manage.py runserver 8003
```

Sonra http://127.0.0.1:8003/products adresini açın.

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
   ekler ve Django'a bağlar. Giriş dosyanız onu bir kez çağırır:
   `application = bootstrap(get_wsgi_application())`.

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
  views.py              # Django view'ları
register/               # hangi sözleşmeler kullanılır - uygulama sınıfı yazmaz
  register.py           # sözleşme + ne kadar yaşar
bootstrap/              # uygulama nasıl ayağa kalkar
  bootstrap.py          # container + kayıtlar + Django köprüsü
shop_site/settings.py   # en sade ayarlar (veritabanı yok)
shop_site/urls.py       # URL yönlendirmeleri
shop_site/wsgi.py       # application = bootstrap(get_wsgi_application())
manage.py               # proje kökünü belirtir
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
from py_autowired import Container
from py_autowired.django import attach_container

from register import register


def bootstrap(application):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    return attach_container(application, container)   # scope per request, close at exit
```

| Yaşam süresi | Kaydedilen sözleşme → kendiliğinden bulunan sınıf | Anlamı |
| --- | --- | --- |
| `register_singleton` | `IClock` → `SystemClock` | bütün uygulama için tek nesne |
| `register_scoped` | `IProductRepository` → `MemoryProductRepository` | HTTP isteği başına bir nesne; sonunda `close()` çağrılır |
| `register_transient` | `IProductService` → `ProductService` | her ihtiyaçta yeni bir nesne |

**Birden çok uygulama mı var?** Bir sözleşmenin iki uygulaması varsa (örneğin
`MemoryProductRepository` ve `SqlProductRepository`) py_autowired tahmin
yürütmez, `AmbiguousImplementationError` hatasıyla durur. O zaman kullanılacak
sınıfı ikinci argüman olarak yazın:
`container.register_scoped(IProductRepository, SqlProductRepository)`.

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

## Yalnız sözleşme: `strict_interfaces=True`

`bootstrap/` içindeki container `Container(strict_interfaces=True)` ile
oluşturulur: yalnız bir sözleşme (arayüz / abstract class) kaydedilebilir,
somut sınıf kaydedilemez. Böylece projedeki her bağımlılık bir soyutlamaya
bağlanır.

## Köprü

`attach_container` WSGI uygulamasını sarar: her istek kendi scope'unu alır, proje ilk istekte taranır, süreç biterken singleton'lar kapanır. `settings.py`'de değişiklik gerekmez; `shop_site/wsgi.py` bir kez `bootstrap(...)` çağırır.

## Kendi projenizde

1. Sınıflarınızı `*_instance = None` alanlarıyla yazın; birbirine import etmeyin.
2. `register/` ve `bootstrap/` klasörlerini kopyalayın; `register/register.py`
   içinde kendi sözleşmelerinizi import edip her sözleşme için bir
   `register_*` satırı yazın.
3. `shop_site/wsgi.py` dosyasındaki gibi `bootstrap(...)`'ı bir kez çağırın.


İsteğe bağlı özellikler (açılışta doğrulama, `Provider`/`Lazy`, anahtarlı
servisler, generic'ler, ...) ana README'de *İsteğe bağlı özellikler*
başlığında anlatılır.
