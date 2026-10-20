# Examples

Four complete, independent projects. Each one has the same layers, a
`register/` folder and its own README that explains that framework step by
step. Nothing is shared between them: copy any one folder and it runs.

| Example | Run (inside the folder) | Read |
| --- | --- | --- |
| [console](console) | `python main.py` | [README](console/README.md) · [Türkçe](console/README.tr.md) |
| [fastapi](fastapi) | `python main.py` or `uvicorn main:app --port 8001` | [README](fastapi/README.md) · [Türkçe](fastapi/README.tr.md) |
| [flask](flask) | `python app.py` or `flask --app app run --port 8002` | [README](flask/README.md) · [Türkçe](flask/README.tr.md) |
| [django](django) | `python manage.py runserver 8003` | [README](django/README.md) · [Türkçe](django/README.tr.md) |

Install first: `pip install -r requirements.txt` (inside the folder).

## The same idea in every example

```text
domain/          contracts (interfaces) - no code
infrastructure/  implementations of the contracts
application/     business logic          - imports nothing, uses *_instance fields
presentation/    controllers and routes  - imports nothing, uses *_instance fields
register/        the ONLY place that imports the classes and sets their lifetimes,
                 and the one call that connects the framework
```

---

# Örnekler

Dört eksiksiz ve birbirinden bağımsız proje. Hepsinde aynı katmanlar, bir
`register/` klasörü ve o framework'ü adım adım anlatan kendi README'si var.
Aralarında paylaşılan hiçbir şey yok: herhangi bir klasörü kopyalayın, çalışır.
Çalıştırma komutları yukarıdaki tabloda; önce klasörün içinde
`pip install -r requirements.txt`.

```text
domain/          sözleşmeler (arayüzler) - kod yok
infrastructure/  sözleşmelerin uygulamaları
application/     iş mantığı               - hiçbir şey import etmez, *_instance alanları kullanır
presentation/    controller ve route'lar  - hiçbir şey import etmez, *_instance alanları kullanır
register/        sınıfları import eden ve yaşam sürelerini belirleyen TEK yer;
                 framework'ü bağlayan tek çağrı da buradadır
```
