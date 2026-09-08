# PinPilot

PinPilot is a local FastAPI foundation for managing products and pins.

## Run locally

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the dashboard and `http://127.0.0.1:8000/health` for health status.

## Etsy bağlantısı (read-only)

PinPilot Etsy Open API v3 için OAuth 2.0 Authorization Code + PKCE akışını destekler. Bağlantı yalnızca mağaza ve aktif listing verilerini okumak içindir; Etsy'ye hiçbir veri yazılmaz veya değiştirilmez.

1. Etsy Developer portalında uygulamanızı oluşturun ve tam olarak `ETSY_REDIRECT_URI` değerini callback URL olarak kaydedin. Etsy callback URL'sinin HTTPS olmasını ve karakter karakter eşleşmesini ister.
2. `.env.example` dosyasını `.env` olarak kopyalayın ve aşağıdaki gerçek değerleri yalnızca yerel `.env` dosyanıza girin:
   - `ETSY_API_KEY`
   - `ETSY_SHARED_SECRET`
   - `ETSY_REDIRECT_URI`
   - `ETSY_TOKEN_ENCRYPTION_KEY` (Fernet anahtarı; `.env.example` içindeki komutla üretilebilir)
3. Uygulamayı başlatın ve dashboard'daki **Connect Etsy** düğmesini kullanın.

İstenen OAuth scope'ları minimum read-only izinler olan `shops_r listings_r` ile sınırlıdır. Erişim ve yenileme token'ları kaynak koda yazılmaz; yerel SQLite veritabanında Fernet ile şifrelenmiş olarak saklanır. Etsy bağlantısını kaldırmak, yerel hesap, token ve eşitlenmiş listing kayıtlarını siler.

## Pinterest bağlantısı

Pinterest API v5 için OAuth 2.0 Authorization Code akışı kullanılır. `.env.example` dosyasını `.env` olarak kopyalayıp aşağıdaki değerleri yalnızca yerel `.env` dosyanıza ekleyin:

- `PINTEREST_CLIENT_ID`
- `PINTEREST_CLIENT_SECRET`
- `PINTEREST_REDIRECT_URI` (Pinterest uygulama ayarlarında kayıtlı değerle birebir aynı olmalı)
- `PINTEREST_TOKEN_ENCRYPTION_KEY` (Fernet anahtarı)

Dashboard'dan **Connect Pinterest** seçeneğini kullanın. CSRF koruması için tek kullanımlık OAuth state değeri sunucu tarafında tutulur. Access ve refresh token'ları kaynak koda yazılmaz ve SQLite içinde Fernet ile şifreli saklanır. İstenen izinler yalnızca `boards:read`, `pins:read` ve ileride Pin yayınlama akışı için `pins:write` değerleridir.

Bu aşamada Pin oluşturma, yayınlama, güncelleme veya silme endpoint'i yoktur. Uygulama yalnızca bağlı hesap ve board bilgilerini GET istekleriyle okur; board'lar dashboard'da ve `/pinterest/boards` JSON endpoint'inde görüntülenir.

## AI Pin creative üretimi

Dashboard'daki **Generate Pin Ideas** bölümü bir ürün ve creative türü için yapılandırılmış Pinterest metni üretir. Desteklenen türler `product_focus`, `lifestyle`, `problem_solution`, `gift_idea` ve `minimalist` değerleridir. Creative'ler taslak olarak saklanır; kullanıcı onları düzenleyebilir, onaylayabilir veya silebilir. Bu aşama Pinterest'e Pin yayınlamaz.

Varsayılan `AI_PROVIDER=mock`, ağ çağrısı ya da API anahtarı gerektirmeyen deterministik test sağlayıcısını kullanır. Gelecekte gerçek bir sağlayıcı eklenmesi için `AIContentProvider` abstraction'ı hazırdır; gerçek sağlayıcı seçildiğinde `AI_API_KEY` yalnızca yerel `.env` dosyasında tanımlanmalıdır. Aynı ürün/tür için hedef sayıya ulaşılmışsa yeni sağlayıcı çağrısı yapılmaz; bu maliyet ve tekrar içeriği önler.

## Current scope

The project provides local SQLite-backed models, a health endpoint, a count dashboard, read-only Etsy listing sync, Pinterest OAuth/account/board read infrastructure, and provider-neutral AI Pin creative generation. Image generation uses the official OpenAI Images API when `AI_IMAGE_PROVIDER=openai`. The dashboard sends the first Etsy listing image to GPT-Image-2 and saves the returned Pinterest-vertical PNG locally under `media/generated`. The generated image is attached to the creative record but is not published to Pinterest yet.

For cost control, one image is requested per creative and the dashboard target remains capped at five. Tests do not make network calls.
