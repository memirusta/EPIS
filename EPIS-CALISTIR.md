# EPIS — Çalıştırma El Kitabı
Cursor olmadan devam için kısa referans.

## Hızlı başlat

1. Ollama kurulu olsun (tray’de çalışır).
2. Çift tık: `D:\EPIS\start_epis.bat`
3. Tarayıcı: http://localhost:8080  
   Model satırı: `qwen-epis (qwen)` olmalı.

Tek pencere: Web UI ön planda; webhook / WhatsApp / Kairos arka planda (loglar `D:\EPIS\logs\`).  
Durdur: pencerede **Ctrl+C** (arka plan süreçleri de kapanır).

## Guvenlik (UI)

- Varsayilan bind: **127.0.0.1** (`EPIS_UI_HOST`) — LAN’daki cihazlar erisemez.
- `/api/*` icin shared secret: `EPIS_UI_SHARED_SECRET` veya `WEBHOOK_SHARED_SECRET`
  (Bearer / `X-EPIS-UI-Secret`; EventSource: `?token=`).
- WhatsApp bridge (3001): **127.0.0.1** — `/send` ve `/qr` LAN’a kapalı.
- Uzak erisim: Tailscale/SSH tüneli; `EPIS_UI_HOST=0.0.0.0` sadece bilinçli aç.

| Servis | Adres |
|--------|--------|
| Web UI | http://localhost:8080 |
| WhatsApp webhook | http://localhost:8000 |
| WhatsApp bridge / QR | http://localhost:3001/qr |
| Ollama | http://localhost:11434 |

## Model değiştirme

`Layer-3\keys.env`:

```
# Fine-tune kimlik modeli (aktif)
QWEN_MODEL=qwen-epis
QWEN_NUM_CTX=16384

# Geri alma (eski base)
# QWEN_MODEL=qwen3.5:9b
```

Değişince EPIS’i yeniden başlat (`Ctrl+C` → `start_epis.bat`).

GGUF’tan modeli yeniden kurmak:

```powershell
cd D:\EPIS\training
ollama create qwen-epis -f Modelfile
```

## Test mesajları (UI)

1. `Hey!` — hata mesajı olmamalı (context 16k)
2. `Sen kimsin?` — EPIS / kullanıcı / Emotional Personalized
3. `Amacin ne?` — birlikte yürümek
4. `Onay makinesi misin?` — hayır / değil
5. `Degerlerin neler?` — dürüstlük / şeffaflık / sadakat yönü
6. `Model degisince sen degisir misin?` — karakter dosyalarda

CLI smoke:

```powershell
python D:\EPIS\training\eval_identity_smoke.py
python D:\EPIS\training\verify_hey_ctx.py
```

Fine-tune yalnızca kimlik/amaç; stil/Türkçe/JSON protokol Layer-2 prompt’ta.

## Önemli dosyalar

| Ne | Nerede |
|----|--------|
| Anahtarlar | `Layer-3\keys.env`, `Layer-3\epis.key` |
| Yedek (tarihli) | `backups\YYYY-MM-DD\` |
| GGUF | `training\out\Qwen2.5-7B-Instruct.Q4_K_M.gguf` (~4.4 GB) |
| Modelfile | `training\Modelfile` |
| Kimlik belgeleri | `Layer-1\identity\` |
| Başlat script | `start_epis.bat`, `scripts\start_epis.ps1` |

`keys.env` / `epis.key` / GGUF → **GitHub’a koyma**.

## Nightly (NC)

`NIGHTLY_OLLAMA_BASE_URL` RunPod pod’una bağlı. Pod kapalıysa NC çalışmaz; yeni pod açıp URL’yi güncelle.

Manuel: `scripts\run_nightly.ps1`

## Ayrı ayrı servisler (gerekirse)

```powershell
D:\EPIS\scripts\start_qwen.ps1
D:\EPIS\scripts\start_epis_ui.ps1
D:\EPIS\scripts\start_webhook.ps1
D:\EPIS\scripts\start_whatsapp_bridge.ps1
D:\EPIS\scripts\start_kairos.ps1
```

## Bilinen sınırlar

- `qwen-epis` varsayılan ctx 4096 idi → `QWEN_NUM_CTX=16384` zorunlu (yoksa UI’da “Bir seyler ters gitti”).
- Kimlik smoke 2–3/5 OK olabilir; WEAK = anahtar kelime kaçtı, sistem bozuk demek değil.
- RunPod pod açık bırakma; saatlik ücret yer.
