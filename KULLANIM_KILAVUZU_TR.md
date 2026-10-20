# py-autowired Türkçe Kullanım Kılavuzu

## Temel kural

Doğrulama artık tipsiz zorunlu kurucu parametrelerini, singleton → scoped
bağımlılıklarını (kurucu, otomatik alan ve factory metadata zincirleri dahil)
ve kalıtılan anotasyonsuz alanları raporlar. Kalıtılan anotasyonsuz alan, alt
sınıfın post-init enjeksiyon adaylarına eklenmez; yeni alan enjekte edilmez.
Temel sınıf kurucusu veya açık atama alanı yine doldurabilir. Bu kontroller
yalnız opt-in doğrulamadır; çalışma zamanı alan seçimi ve sırası değişmez.

Eşit puanlı adaylarda ilk kayıt seçilir; bu durum çalışma anında loglanmaz,
yalnız isteğe bağlı doğrulama (`validate=True`) raporlar.
Alan okuma hatası traceback ile WARNING üretir ve alan yine atlanır.
Keşif, import edilen dosyanın beklenen dosya olduğunu kontrol eder; uyuşmazlıkta
WARNING yazar, mevcut modülü değiştirmez ve importu başarılı saymaz.
Senkron factory çıplak bir coroutine döndürürse `AsyncFactoryError` yükselir ve
coroutine kapatılır; diğer awaitable nesneler (havuz, future) olduğu gibi döner.
Birden çok kapanış hatasında ilk hata aynı tip ve nesneyle yükselir; diğerleri not
olarak eklenir ve loglanır. Aday seçimi: alan adı zaten qualifier gibi çalışır (`utc_clock_instance` → `UtcClock`).
Birden fazla kayıt eşleşirse tercih edileni `container.set_primary(UtcClock)` ile işaretleyin
(Spring `@Primary` gibi); işaret yoksa seçim eskisi gibidir. Aynı tipe birden çok uygulama anahtarla kaydedilebilir: `register_keyed(IClock, "utc", UtcClock)`,
`resolve_keyed(IClock, "utc")` ve kurucuda `Annotated[IClock, Keyed("utc")]`. Anahtarlı kayıtlar
`*_instance` alan eşleşmesine katılmaz.
Açık generic kayıt (isteğe bağlı): `register_open_generic(Repository, SqlRepository)` ile
`Repository[User]`, `Repository[Order]`... istendiğinde üretilir; her kapalı tipin kendi yaşam
süresi vardır. Bunlar `*_instance` alan eşleşmesine katılmaz.
Ertelenmiş çözümleme (isteğe bağlı): kurucu parametresini `Provider[T]` (her çağrıda
çözer, o anki scope'a uyar) ya da `Lazy[T]` (ilk `.value` erişiminde bir kez çözer)
olarak işaretleyin. Singleton, `Provider[ScopedServis]` tutarsa scoped servisi esir almaz.
Uygulama kapanışı isteğe bağlıdır: iş bittikten sonra `container.close()` ya da
`await container.aclose()` çağırın; container'ın oluşturduğu singleton'lar ters
sırayla kapatılır, `register_instance` ile verilenler size aittir ve kapatılmaz.
Eşzamanlılık: hazır singleton'lar ve önbellek isabetleri kilitsizdir. İki thread
çevrimli singleton grafiğini aynı anda ilk kez çözse bile donma olmaz,
`CircularDependencyError` yükselir. `refresh()` eski seçimi önbellekte bırakmaz;
scope kapanışı, oluşturulmakta olan servisi de kapatır. Çevrimi açılışta
`validate=True` ile yakalayın.

Runtime bağımlılığı constructor parametresi veya manuel container çağrısı değildir:

```python
class SiparisServisi:
    siparis_repository_instance: SiparisRepository | None = None
```

`auto_inject()`, alan adındaki `siparis_repository` bölümünü kayıtlı
`ISiparisRepository` veya `SiparisRepository` tipiyle eşleştirir ve nesneyi
çalışma anında yerleştirir.
Repository tipi önceden import edilmiş olmalıdır (veya ertelenmiş tip etiketleri
kullanılmalıdır). Tip etiketi açıklama ve araç desteği içindir; eşleştirme yine
alan adı ve modül yakınlığıyla yapılır. `= None` gereklidir: tek başına tip etiketi
alanı başlatmaz. `self.siparis_repository_instance = None` bildirimi de desteklenir;
açıkça atanmış `None` dışındaki değerler korunur. Tip denetleyicileri alanı
kullanmadan önce bir `None` kontrolü isteyebilir.

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

## Başlangıçta doğrulama (Validate at startup)

Doğrulama varsayılanda kapalıdır. Açmak için
`auto_inject(container, root_dir=..., validate=True)` veya
`attach_container(app, container, root_dir=..., validate=True)` kullanın.
FastAPI denetimi lifespan başlangıcında yapar. Yamalama sonrasında
`from py_autowired import validate_autowiring; validate_autowiring(container)`
ile tekrar denetleyebilirsiniz; bu fonksiyon yeni sınıf keşfetmez veya yamalamaz.

`AutowiringValidationError.issues`, eşleşmeyen alanları, aynı en yüksek puanı alan
farklı kayıtları ve statik olarak çözülemeyen tipleri tek raporda toplar.
Aynı kaydın alias tekrarları belirsizlik sayılmaz; modül yakınlığı tek kazanan
belirliyorsa hata verilmez. Denetim nesne/servis oluşturmaz, factory ve alan descriptor'ı
çalıştırmaz, scope açmaz, çalışma zamanı çözümleme önbelleğini değiştirmez.
Tip açıklamalarını değerlendirebilir; bu ifadeler yan etkili kullanıcı kodu çalıştırabilir.
Zorunlu constructor bağımlılıkları, soyut tipler, döngüler ve
`factory_dependencies` bilgisi incelenir. Factory gövdeleri, keyfi constructor
kodu ve özel setter davranışları statik olarak garanti edilemez.
Constructor içinde elle atanacak değerler önceden bilinemez; aday alanlar temkinli
denetlenir. Sınıf düzeyindeki somut elle atamalar atlanır. `auto_inject` sırasında
doğrulama başarısız olsa da yamalar kurulmuş kalır; grafiği düzeltin veya uygulama
başlangıcını durdurun.

## Testte servis değiştirme (Testing: swap a service)

Yukarıdaki `SiparisServisi` ve mevcut `SiparisRepository` için pytest tarifi:

```python
from py_autowired import Container, auto_inject

class SahteSiparisRepository(SiparisRepository):
    pass

def test_degistirme():
    container = Container()
    container.register_transient(SiparisRepository)
    container.replace(SiparisRepository, SahteSiparisRepository)
    auto_inject(container, root_dir="/uygulamanin/mutlak/yolu", validate=True)
    servis = SiparisServisi()
    assert isinstance(servis.siparis_repository_instance, SahteSiparisRepository)
```

Her testte yeni container kullanın. Değiştirme otomatik enjeksiyon kurulduktan sonra
yapılıyorsa yeni tüketicileri oluşturmadan önce
`from py_autowired import refresh; refresh()` çağırın. Mevcut nesneler atanmış
bağımlılıklarını korur. Etkin container süreç genelinde tek olduğundan farklı
container kullanan testleri sıralı çalıştırın.

## Uyumluluk (Compatibility)

`tests/test_field_compatibility.py` ile Python 3.11 ve Pydantic 2.14.1 üzerinde
ölçülen davranışlar:

| Sınıf biçimi | Sonuç |
| --- | --- |
| `= None` içeren tip etiketli normal sınıf | Çalışır; açık `None` dışı atamalar korunur. |
| Değiştirilebilir dataclass, `slots=True` dahil | Dekoratör sonrasında çalışır; constructor'a verilen elle atamalar korunur. |
| `__slots__` | Alanın slot'u mevcut ve `__init__` içinde `None` ile başlatılmışsa çalışır; başlatılmamış slot boş kalır. |
| Frozen dataclass, slot'lu/slot'suz | Desteklenmez; post-init ataması `AutoInjectionResolutionError` verir. |
| Değiştirilebilir Pydantic v2 `BaseModel` | Servis tipleri için `arbitrary_types_allowed=True` ile çalışır; alanlar model doğrulaması ve `model_post_init` sonrasında dolar, yeniden tam model doğrulaması yapılmaz. |
| Frozen Pydantic v2 | Desteklenmez; atama `AutoInjectionResolutionError` verir. |
| `attrs` | Yerelde kurulu değil; değiştirilebilir/slot'lu/frozen testleri atlandı, uyumluluk doğrulanmadı. |

Özgün atama hataları exception cause olarak korunur. Özel descriptor/setter'lar,
başka model ayarları ve Pydantic v1 bu ölçümlerin kapsamında değildir. Statik
doğrulama değişmez modelleri yazılabilir hâle getirmez.

## Günlükleme ve performans ölçümü

Kütüphane İngilizce iletileri `logging.getLogger("py_autowired")` üzerinden yazar.
Varsayılanda yalnız `NullHandler` eklenir; keşif hatalarını görmek için uygulama
logging ayarını yapın. Import hataları her zaman WARNING kaydı üretir, hata
fırlatmaz ve Python `warnings` mekanizmasını kullanmaz. Import öncesinde
`PY_AUTOWIRED_DEBUG=1` (veya yedek `DI_DEBUG=1`) ayarı DEBUG iletilerini ve stderr'e
yazan kütüphane `StreamHandler`'ını açar. Normal işlem stdout'a yazmaz.
`print_cache_stats()` stdout'a yazar (değişmedi).

Yerel ölçüm: `python -B benchmarks/field_injection.py --number 100000 --repeat 5`.
Windows AMD64 / Python 3.11.0 örneği: düz nesne 0,227 µs, otomatik enjeksiyon
2,721 µs; ek süre 2,494 µs (toplam 11,97 kat). Beş tekrarın en iyisi alınmıştır;
tek alan, hazır singleton ve sıcak önbellek ölçülür. Başlangıç ve bağımlılık
oluşturma maliyeti dahil değildir. Sonuç makinenize göre değişir; Python sürümü
ve bağımlılık grafiği de etkilidir. Performans garantisi değildir.

## Yaşam süreleri

- `register_singleton`: Uygulama süresince aynı nesne.
- `register_scoped`: Bir scope içinde aynı, sonraki scope'ta yeni nesne.
- `register_transient`: Her enjeksiyonda yeni nesne.
- `register_instance`: Önceden oluşturulmuş nesneyi kullanır.
- `register_factory`: Nesneyi `factory(container)` çağrısıyla oluşturur; factory
  kullanmasa bile container tek argüman olarak her zaman geçirilir.

```python
container.register_factory(UserService, lambda container: UserService())
```

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

## FastAPI köprüsü ve kapsam sınırı

`py-autowired[fastapi]` ile kayıtları tamamladıktan sonra köprüyü bağlayın:

```python
from fastapi import FastAPI
from py_autowired.fastapi import attach_container

app = FastAPI()
attach_container(app, container, root_dir="/uygulamanin/mutlak/yolu")

@app.get("/users")
def users():
    return UserController().execute()
```

Başlangıçta otomatik alan enjeksiyonu etkinleşir. Endpoint içinde yalnız nesne
oluşturmanız yeterlidir; alanları kendiliğinden dolar. Her HTTP isteği ayrı
kapsam kullanır; streaming ve arka plan işleri tamamlanınca kaynaklar kapatılır.
Asenkron kapanış aktif AnyIO iptalinden korunur. WebSocket bağlantıları bu HTTP
köprüsünün kapsamı dışındadır.

İstek kapsamı Starlette ara katman yığınının tamamını sarar. Kullanıcı ara
katmanları ve genel `Exception`/500 dahil tüm istisna işleyicileri aynı açık
kapsamı paylaşır ve istek kapsamlı otomatik enjeksiyonu kullanabilir. Kapsam,
yanıt ve arka plan işleri tamamlandıktan sonra kapanır.

FastAPI tarzı isteğe bağlı parametre enjeksiyonu için `fastapi` modülünden
`Depends`, `py_autowired.fastapi` modülünden `injected` import ederek
`controller=Depends(injected(UserController))` kullanabilirsiniz.

## Etkin container ve yaşam süresi önerisi

Her süreçte tek etkin otomatik enjeksiyon container'ı desteklenir. Sınıf yamaları
ve etkin isim haritası süreç genelindedir; farklı container kullanan eşzamanlı
uygulamaları ayrı süreçlerde çalıştırın. Aynı container içindeki istek kapsamları
context değişkenleriyle birbirinden ayrılır.

**Yaşam süresi uyarısı:** Singleton bir nesnenin scoped servisi alanında tutması
önerilmez; scoped sahip kullanın. Böylece istek kaynakları kapsam kapandıktan
sonra tutulmaz. Bu yalnızca belge uyarısıdır; çalışma zamanı davranışı değişmez.

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

Bağımlılığı `self.repository_instance = None` atamasıyla veya
`repository_instance = None` sınıf alanıyla bildirin. Açıkça atanmış `None`
dışındaki değerler korunur; kayıtla eşleşmeyen alanlar `None` kalır. Her iki
bildirim biçimi aynı servis eşleştirme ve yaşam süresi kurallarını kullanır.

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
