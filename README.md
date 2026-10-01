# PinPilot

PinPilot is a local FastAPI foundation for managing products and pins.

## Run locally

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the dashboard and `http://127.0.0.1:8000/health` for health status.

## Dashboard administrator access

The dashboard's existing read-only views remain available without signing in.
All state-changing browser/API operations require the configured single admin
session and a CSRF token. Before enabling dashboard mutations, provide
`APP_AUTH_USERNAME`, `APP_AUTH_PASSWORD` (at least 16 characters), and
`APP_SESSION_SECRET_KEY` (at least 32 bytes) through the deployment's protected
environment/secret store. The application does not generate or print secrets;
missing or invalid settings deny login and protected operations. Sessions expire
after `APP_SESSION_TTL_SECONDS` (default eight hours) and use HttpOnly,
SameSite=Lax cookies; `APP_SESSION_COOKIE_SECURE` defaults to `true`, so a
working HTTPS deployment is required for browser sessions. OAuth provider
callbacks remain protected by their existing single-use server-side OAuth
state checks.

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

Dashboard'dan **Connect Pinterest** seçeneğini kullanın. CSRF koruması için tek kullanımlık OAuth state değeri sunucu tarafında tutulur. Access ve refresh token'ları kaynak koda yazılmaz ve SQLite içinde Fernet ile şifreli saklanır. İstenen minimum izinler `user_accounts:read`, `boards:read`, `pins:read` ve Pin yayınlama hazırlığı için `pins:write` değerleridir. `boards:write` istenmez.

Pinterest v5 HTTP adapter'ı resmi API hostlarını kullanır; production varsayılandır. İzole Sandbox için `.env` içinde `PINTEREST_API_BASE_URL=https://api-sandbox.pinterest.com/v5` ayarlanabilir. İstemci timeout uygular, hata sınıflarını ayırır ve token/secret değerlerini loglamaz. Create Pin görsel kaynağı `image_url` biçimindedir; yerel oluşturulmuş dosyalar yalnızca `PUBLIC_BASE_URL` üzerinden herkese açık HTTPS URL'ye çevrilir. Yerel dosya yolu veya güvenli olmayan URL reddedilir.

API adapter'ı hesap/board okuma, Pin oluşturma/okuma/güncelleme/silme işlemlerini destekler. `PinterestPublisher` API sağlayıcısı `PINTEREST_PUBLISH_ENABLED=false` varsayılanıyla kapalıdır ve mevcut scheduler/worker'a bağlanmamıştır. Bu nedenle bu aşamada Pin yayınlama otomatik başlamaz. Başarılı bir API Create Pin yanıtındaki dış Pin kimliğini yerel `PublishedPinterestPin` kaydına aktarma işi mevcut idempotent publisher koordinatöründe tamamlanır.

## AI Pin creative üretimi

Dashboard'daki **Generate Pin Ideas** bölümü bir ürün ve creative türü için yapılandırılmış Pinterest metni üretir. Desteklenen türler `product_focus`, `lifestyle`, `problem_solution`, `gift_idea` ve `minimalist` değerleridir. Creative'ler taslak olarak saklanır; kullanıcı onları düzenleyebilir, onaylayabilir veya silebilir. Bu aşama Pinterest'e Pin yayınlamaz.

Varsayılan `AI_PROVIDER=mock`, ağ çağrısı ya da API anahtarı gerektirmeyen deterministik test sağlayıcısını kullanır. Gelecekte gerçek bir sağlayıcı eklenmesi için `AIContentProvider` abstraction'ı hazırdır; gerçek sağlayıcı seçildiğinde `AI_API_KEY` yalnızca yerel `.env` dosyasında tanımlanmalıdır. Aynı ürün/tür için hedef sayıya ulaşılmışsa yeni sağlayıcı çağrısı yapılmaz; bu maliyet ve tekrar içeriği önler.

## Current scope

The project provides local SQLite-backed models, a health endpoint, a count dashboard, read-only Etsy listing sync, Pinterest OAuth/account/board read infrastructure, and provider-neutral AI Pin creative generation. Image generation uses the official OpenAI Images API when `AI_IMAGE_PROVIDER=openai`. The dashboard sends the first Etsy listing image to GPT-Image-2 and saves the returned Pinterest-vertical PNG locally under `media/generated`. The generated image is attached to the creative record but is not published to Pinterest yet.

For cost control, one image is requested per creative and the dashboard target remains capped at five. Tests do not make network calls.

## Public HTTPS generated media

Generated images are stored under `media/generated` and are internally addressed
as `/media/generated/<filename>.png`. Pinterest must receive an absolute HTTPS
URL, for example `https://pins.example.com/media/generated/<filename>.png`.

The VPS Nginx configuration currently exposes media over HTTP only. The prepared
template at `deploy/nginx/pinpilot-https.conf.example` serves only
`/media/generated/`, redirects HTTP to HTTPS, and does not expose project files
or the database. To activate it, use an existing domain/subdomain pointed at the
VPS, obtain a free Let’s Encrypt certificate, then set this in the VPS `.env`:

```text
PUBLIC_BASE_URL=https://pins.example.com
```

No temporary Cloudflare tunnel is required. Cloudflare’s free DNS/proxy can be
used if a domain is already managed there, but it does not replace ownership of
a stable domain. Until a stable HTTPS hostname is configured, PinPilot safely
retains relative media paths and future Pinterest publishing must not use them.

## Production daily Pin queue

The scheduler is a local-only systemd oneshot job. It creates up to the existing
daily target of 15 Pin records using mockups first, then existing AI creatives,
and can queue local pending AI work for any gap. It does not call Etsy, Gemini,
OpenAI, or Pinterest.

On the current UTC-configured VPS, `pinpilot-daily-queue.timer` runs daily at
00:05 UTC. `Persistent=true` causes one missed run to execute after a reboot.
Install the tracked unit files after deployment, then enable the timer:

```bash
cp deploy/systemd/pinpilot-daily-queue.service /etc/systemd/system/
cp deploy/systemd/pinpilot-daily-queue.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pinpilot-daily-queue.timer
systemctl list-timers pinpilot-daily-queue.timer
```

Failures are available through `journalctl -u pinpilot-daily-queue.service` and
do not stop the independent `pinpilot.service` web application.

## Optional Pinterest analytics collection

The daily analytics collector and its systemd units are prepared, but are not
enabled by default. `PINTEREST_ANALYTICS_COLLECTION_ENABLED` defaults to
`false`; while it is false, the scheduled entry point exits before creating a
Pinterest API provider or reading OAuth credentials. Do not enable collection
until Pinterest API access and the required account permissions have been
confirmed. When explicitly approved, set the flag in the service environment,
install `deploy/systemd/pinpilot-analytics-collection.service` and
`deploy/systemd/pinpilot-analytics-collection.timer`, then enable the timer.
The job refreshes the last seven complete UTC dates and reuses the persisted
collection run for each account/date when retried. Its logs contain only run,
failure, and snapshot counts.
