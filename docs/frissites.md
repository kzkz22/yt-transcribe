# Frissítés és karbantartás

Komponensenként leírja, hogyan frissíts, hogyan ellenőrizd az eredményt, és hogyan lépj vissza, ha baj van. A telepítés lépései a [telepítési leírásban](telepites.md) vannak.

## Általános szabályok

- **Egyszerre egy komponenst frissíts,** és utána ellenőrizd. Így ha valami elromlik, tudod, mi volt az oka.
- **A repót frissítsd először** ott, ahonnan a fájlokat másolod:
  ```bash
  git -C /root/src/yt-transcribe pull          # az ai-router LXC-ben
  ```
  Az Unraidon, ahol nincs git, töltsd le újra (lásd [telepítés](telepites.md#amire-szükség-lesz)).
- **A gépen lévő beállítófájlokat ne írd felül vakon.** A `/etc/...` alatti fájlokban a saját kulcsaid és címeid vannak. Előbb nézd meg a különbséget, és csak az új részeket vedd át:
  ```bash
  diff -u /etc/llama-swap/config.yaml /root/src/yt-transcribe/deploy/llama-swap/config.yaml
  ```
- **Mentés:** frissítés előtt mentsd ezeket:
  - az `ai-router` LXC-ben: `/etc/llama-swap/`, `/etc/litellm/`, `/etc/yt-transcribe/`;
  - az Unraidon: `/mnt/user/appdata/llama.cpp/config/models.ini`.

  Az LXC-ről egyszerűbb egy Proxmox-pillanatkép (snapshot), a visszalépés így egy kattintás.

## Verziók áttekintése

| Komponens | Hol van rögzítve | Verzió lekérdezése |
| --- | --- | --- |
| llama-swap | a letöltött bináris (`/opt/llama-swap/llama-swap`) | `/opt/llama-swap/llama-swap -version` |
| LiteLLM | `pip install 'litellm[proxy]==…'` | `/opt/litellm/venv/bin/pip show litellm` |
| yt-transcribe API | a repó állapota | `git -C /root/src/yt-transcribe log -1 --oneline` |
| yt-dlp | nincs rögzítve, induláskor frissül | `/opt/yt-transcribe/venv/bin/yt-dlp --version` |
| deno | a letöltött bináris | `deno --version` |
| GPU-worker | a repó állapota; benne `whisperx==3.8.6` | `docker image inspect yt-transcribe-gpu --format '{{.Created}}'` |
| llama.cpp | az image címkéje (`server-cuda`) | `docker image inspect ghcr.io/ggml-org/llama.cpp:server-cuda --format '{{.Created}}'` |

## llama.cpp (Unraid)

A `server-cuda` címke mindig a legújabb buildre mutat.

1. Állítsd le a konténert a llama-swapon keresztül, hogy közben ne kapjon kérést:
   ```bash
   # az ai-router LXC-ben
   runuser -u llamaswap -- /opt/llama-swap/unraid-container.sh stop llama.cpp
   ```
2. Az Unraid Docker fülén: `llama.cpp` → **Force Update** (vagy az Advanced View-ban a frissítés). Ez letölti az új image-et, és a sablon alapján újra létrehozza a konténert.
3. Ha a frissítés után a konténer fut, állítsd le. A llama-swap indítja majd, amikor kell.
4. **Ellenőrzés:** egy kérés egy helyi modellnévre (lásd [telepítés 2.2](telepites.md#22-llama-swap)).

**Visszalépés:** a sablonban a Repository mezőben adj meg egy konkrét buildet, például `ghcr.io/ggml-org/llama.cpp:server-cuda-b<SZÁM>`, majd Apply. A buildszámot a llama.cpp GitHub-oldalának Releases részében találod.

## Preset vagy modell hozzáadása, átnevezése

Egy új preset négy helyen jelenik meg. Ha valamelyik kimarad, a kliensben nem látszik, vagy hibát ad.

1. **`models.ini`** (Unraid, `/mnt/user/appdata/llama.cpp/config/`): új szakasz `[PresetNév]` fejléccel, `model`, `alias` és a többi beállítás, a meglévők mintájára. A router a presetek listáját induláskor olvassa be, ezért utána állítsd le a konténert:
   ```bash
   runuser -u llamaswap -- /opt/llama-swap/unraid-container.sh stop llama.cpp
   ```
2. **llama-swap** (`/etc/llama-swap/config.yaml`): a presetnevet vedd fel a `llama-server` bejegyzés `aliases` listájába. Mentés után a llama-swap magától újraolvassa (`--watch-config`).
3. **LiteLLM** (`/etc/litellm/config.yaml`): új elem a `model_list`-be, a meglévők mintájára:
   ```yaml
   - model_name: UjPreset-Neve
     model_info: { max_input_tokens: 180000, max_output_tokens: 32768, supports_vision: true }
     litellm_params: { <<: *local, model: openai/UjPreset-Neve }
   ```
   - A `max_input_tokens` a preset `ctx-size` értéke.
   - A `supports_vision` akkor `true`, ha a presetnek van `mmproj` sora.

   Utána: `systemctl restart litellm`.
4. **OpenCode** (`opencode.json`): új elem a `models` alá, ugyanazokkal az értékekkel.

Végül a repóban is frissítsd a `deploy/` fájlokat, hogy a következő telepítés is egyezzen.

**Ellenőrzés:** `curl localhost:4000/v1/models -H "Authorization: Bearer $K"` kilistázza az új nevet, és egy kérés rá választ ad.

## Új GPU-s konténer bekötése

Bármilyen új program, ami a GPU-t használja (másik LLM-motor, képgenerátor), ugyanúgy kerül a llama-swap alá, mint a llama-server és a GPU-worker. Ha kimarad, a llama-swap nem tud róla, és két program foglalhatja egyszerre a VRAM-ot.

1. **Unraid:** a konténer kapjon fix portot, és az automatikus indítása legyen kikapcsolva. Legyen egy címe, ami csak akkor ad 200-as választ, ha a program már kész kéréseket fogadni (llama.cpp-nél ez a `/health`).
2. **`/etc/llama-swap/unraid.env`:** a konténer nevét vedd fel a `GPU_CONTAINERS` listába. Az indítóscript így leállítja, mielőtt mást indít.
3. **`/etc/llama-swap/config.yaml`:** új bejegyzés a `models:` alá, a meglévők mintájára:
   ```yaml
   uj-motor:
     cmd: /opt/llama-swap/unraid-container.sh run KONTÉNERNÉV
     cmdStop: /opt/llama-swap/unraid-container.sh stop KONTÉNERNÉV ${PID}
     proxy: http://192.168.1.20:PORT
     checkEndpoint: /health
     aliases: [a kliensek által használt modellnevek]
   ```
4. **LiteLLM:** a modellneveket vedd fel a `model_list`-be (lásd az előző pontot), és indítsd újra.

**Lassan induló programnál** (percekig tölt) ezekre figyelj:
- A llama-swap `healthCheckTimeout` értéke (a `config.yaml` elején, most 180 másodperc) közös minden bejegyzésre. Legyen nagyobb, mint a leglassabb indulás, különben a llama-swap hibának veszi.
- A LiteLLM a helyi modelleknél 600 másodpercig vár az első tokenre (`stream_timeout`). Ebbe a modellváltás teljes ideje is beleszámít, a futó kérések kivárásával együtt.
- A kliensnek is ki kell várnia: egy 4 perces indulásnál sok kliens alapértelmezett időkorlátja lejár.
- Minden modellváltás újra kifizeti az indulási időt, az átírás utáni visszaváltás is. A Strata ezért maradt ki: 3,5–4 percig indult (lásd az [architektúra-leírást](architektura.md#kipróbált-de-kimaradt-strata)).

## llama-swap (ai-router LXC)

1. Töltsd le az új verziót egy ideiglenes helyre, és ellenőrizd vele a mostani konfigurációt:
   ```bash
   V=263   # az új verzió száma a GitHub Releases oldalról
   cd /tmp && curl -fsSLO https://github.com/mostlygeek/llama-swap/releases/download/v$V/llama-swap_${V}_linux_amd64.tar.gz
   mkdir -p /tmp/ls-new && tar xzf llama-swap_${V}_linux_amd64.tar.gz -C /tmp/ls-new llama-swap
   /tmp/ls-new/llama-swap -version
   /tmp/ls-new/llama-swap -config /etc/llama-swap/config.yaml -validate
   ```
2. Ha a konfiguráció érvényes, cseréld le, de a régit tartsd meg:
   ```bash
   cp /opt/llama-swap/llama-swap /opt/llama-swap/llama-swap.prev
   install -m 755 /tmp/ls-new/llama-swap /opt/llama-swap/llama-swap
   systemctl restart llama-swap
   ```
   Újraindításkor a futó konténer tovább fut. Az indítóscript a következő kéréskor átveszi, nem indítja újra.
3. **Ellenőrzés:** `journalctl -u llama-swap -n 30`, majd egy kérés egy helyi modellnévre.

**Visszalépés:** `install -m 755 /opt/llama-swap/llama-swap.prev /opt/llama-swap/llama-swap && systemctl restart llama-swap`

A kiadási jegyzetekben (GitHub Releases) figyelj a `cmd`, `cmdStop`, `checkEndpoint`, `aliases`, `ttl` és `/upstream` változásaira: ezekre épül a rendszer.

### Az indítóscript (`unraid-container.sh`)

```bash
diff -u /opt/llama-swap/unraid-container.sh /root/src/yt-transcribe/deploy/llama-swap/unraid-container.sh
install -m 755 /root/src/yt-transcribe/deploy/llama-swap/unraid-container.sh /opt/llama-swap/
```

Újraindítás nem kell, a llama-swap minden indításnál újra lefuttatja.

## LiteLLM (ai-router LXC)

A verzió rögzítve van, mert a LiteLLM gyakran ad ki új verziót, és a változások a beállításokat is érinthetik.

1. Nézd meg a kiadási jegyzeteket (GitHub, BerriAI/litellm), különösen a `fallbacks`, `model_info` és `router_settings` változásait.
2. Frissítés, a régi verzió számát feljegyezve:
   ```bash
   /opt/litellm/venv/bin/pip show litellm | grep Version      # a mostani
   /opt/litellm/venv/bin/pip install 'litellm[proxy]==ÚJ_VERZIÓ'
   systemctl restart litellm
   ```
3. **Ellenőrzés:** a `/v1/models` lista, egy helyi és egy `cloud/…` kérés, majd a felhőre váltás próbája (`ip route add unreachable …`, lásd [telepítés 2.3](telepites.md#23-litellm)).

**Visszalépés:** `pip install 'litellm[proxy]==RÉGI_VERZIÓ'` és újraindítás.

### Felhős modell cseréje

Egy OpenRouter-modell cseréjéhez vagy hozzáadásához:
1. Írd át a `/etc/litellm/config.yaml`-ban a `cloud/…` bejegyzést, a `model: openrouter/<szolgáltató>/<modell>` formában. Ha az `auto` vagy az `osszefoglalo` tartalékát cseréled, a `fallbacks` sorát is.
2. `systemctl restart litellm`.
3. Az OpenCode `opencode.json`-jában is frissítsd.

## yt-transcribe API (ai-router LXC)

```bash
git -C /root/src/yt-transcribe pull
rsync -a --delete /root/src/yt-transcribe/api/ /opt/yt-transcribe/api/     # vagy: rm -rf + cp -r
/opt/yt-transcribe/venv/bin/pip install -r /opt/yt-transcribe/api/requirements.txt
diff -u /etc/yt-transcribe/api.env /root/src/yt-transcribe/deploy/yt-transcribe/api.env.example
diff -u /etc/systemd/system/yt-transcribe-api.service /root/src/yt-transcribe/deploy/yt-transcribe/yt-transcribe-api.service
systemctl daemon-reload && systemctl restart yt-transcribe-api
```

Futó átírás közben ne indítsd újra. A `/health` válaszában a `jobs_waiting` mutatja, van-e munkában valami.

**Ellenőrzés:** `curl -s localhost:8765/health`, majd egy rövid videó átírása (lásd [telepítés 2.4](telepites.md#24-yt-transcribe-api)).

**Visszalépés:** `git -C /root/src/yt-transcribe checkout <előző commit>`, és ugyanezek a lépések.

A gyorsítótárban lévő átiratok (`/var/lib/yt-transcribe/transcripts`) frissítés után is megmaradnak.

### yt-dlp és deno

- A **yt-dlp** a szolgáltatás minden indításakor frissül, a `curl-cffi` kiegészítővel együtt (ezzel a yt-dlp böngészőnek látszik, és ritkábban kap 429-es tiltást). Ha egy YouTube-videó vagy felirat letöltése hibát ad, először ezt próbáld: `systemctl restart yt-transcribe-api`.
- **Ha a szolgáltatásfájl még a régi** (`yt-dlp[default]` szerepel benne `curl-cffi` nélkül), másold át az újat a repóból (`deploy/yt-transcribe/yt-transcribe-api.service`), majd `systemctl daemon-reload`.
- A **deno**-t ritkán kell frissíteni, csak ha a yt-dlp hibaüzenete erre utal: `deno upgrade`.

## GPU-worker (Unraid)

1. Friss forrás és új image:
   ```bash
   cd /root/src && rm -rf yt-transcribe && wget -qO- https://github.com/kzkz22/yt-transcribe/archive/refs/heads/main.tar.gz | tar xz && mv yt-transcribe-main yt-transcribe
   rm -rf /mnt/my_new_cache/appdata/yt-transcribe/gpu-worker
   cp -r /root/src/yt-transcribe/gpu-worker /mnt/my_new_cache/appdata/yt-transcribe/
   cd /mnt/my_new_cache/appdata/yt-transcribe
   docker build -t yt-transcribe-gpu ./gpu-worker
   ```
2. Állítsd le a workert a llama-swapon keresztül (az ai-router LXC-ben), hogy futó munkát ne szakíts meg:
   ```bash
   runuser -u llamaswap -- /opt/llama-swap/unraid-container.sh stop yt-transcribe-gpu
   ```
3. Hozd létre újra a konténert ugyanazzal a paranccsal, mint telepítéskor. A `models` és a `worker-data` mappa megmarad.
   ```bash
   docker rm yt-transcribe-gpu
   docker create --name yt-transcribe-gpu --gpus all \
     -p 8766:8766 \
     -v /mnt/my_new_cache/appdata/yt-transcribe/models:/models \
     -v /mnt/my_new_cache/appdata/yt-transcribe/worker-data:/data \
     -e HF_TOKEN=hf_IDE_A_TOKENED \
     yt-transcribe-gpu
   docker image prune -f        # a régi, címke nélküli image törlése
   ```
4. **Ellenőrzés:** egy rövid videó átírása helyi módban.

A worker egy beállításának (például a `HF_TOKEN`-nek) a cseréjéhez is a 2–3. lépés kell: a konténert a beállítással együtt újra kell létrehozni.

**Visszalépés:** építsd újra az image-et az előző commitból, és hozd létre újra a konténert.

A `whisperx` verziója szándékosan rögzített (`gpu-worker/requirements.txt`), mert a kód az adott verzió függvényeire épül. Új verzióra csak a repóban, a tesztek lefuttatása után térj át (`python -m pytest -q tests`).

## Hermes skill (Windows)

Másold a repó `hermes-skill/yt-transcribe` mappáját a régi helyére (`%LOCALAPPDATA%\hermes\skills\media\yt-transcribe`), felülírva a régit. A beállítások (`skills.config.yt_transcribe.*`) a Hermes saját konfigurációjában vannak, megmaradnak.

## Operációs rendszerek

- **ai-router LXC:** `apt update && apt full-upgrade -y`, utána `systemctl restart llama-swap litellm yt-transcribe-api`. Debian főverzió-váltásnál (például 12 → 13) a Python verziója is változik. Ilyenkor a két venv-et (`/opt/litellm/venv`, `/opt/yt-transcribe/venv`) újra kell létrehozni a telepítési leírás szerint.
- **Proxmox és Unraid:** a megszokott módon. Az Unraid frissítése után ellenőrizd, hogy az API-kulcs megvan-e (Settings → Management Access → API Keys), és hogy a két GPU-s konténer automatikus indítása kikapcsolva maradt-e.
