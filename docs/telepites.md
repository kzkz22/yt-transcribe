# Telepítés lépésről lépésre

Ez a leírás a teljes rendszert telepíti a [architektúra](architektura.md) szerint: Unraid, Proxmox LXC, kliensek. A sorrend számít: minden lépés az előzőre épül, és mindegyik végén van egy ellenőrzés.

A példákban szereplő címek és nevek:

| Mi | Érték a példákban |
| --- | --- |
| Unraid | `192.168.1.20` |
| Proxmox LXC | `ai-router`, a címe a leírásban `AI-ROUTER-IP` |
| Unraid appdata | `/mnt/my_new_cache/appdata` |
| llama.cpp konténer | `llama.cpp`, port 8001 |
| GPU-worker konténer | `yt-transcribe-gpu`, port 8766 |

Ha nálad más, a fájlokban is írd át. A címek a `deploy/llama-swap/config.yaml`, a `deploy/llama-swap/unraid.env.example` és a `deploy/clients/opencode.json` fájlban vannak.

## Amire szükség lesz

- **Unraid API-kulcs** (az 1.1 lépésben készül).
- **Hugging Face token** a beszélőfelismeréshez:
  1. A huggingface.co-n fogadd el a `pyannote/speaker-diarization-community-1` feltételeit.
  2. Készíts egy *read* tokent: huggingface.co/settings/tokens.
- **OpenRouter API-kulcs** a felhős LLM-hez: openrouter.ai/settings/keys. Az OpenRouter Privacy beállításaiban érdemes kizárni azokat a szolgáltatókat, amelyek tanítanak a beküldött adatokon.
- **AssemblyAI API-kulcs**, ha felhős átírást is akarsz (assemblyai.com). Nélküle a felhős átírás egyszerűen ki van kapcsolva.
- **A repó.** Ahol fájlt kell másolni, ott a forrás ez a repó. A legegyszerűbb, ha a gépre letöltöd:
  ```bash
  git clone https://github.com/kzkz22/yt-transcribe /root/src/yt-transcribe
  ```
  Ahol nincs git (például az Unraidon):
  ```bash
  mkdir -p /root/src && cd /root/src && wget -qO- https://github.com/kzkz22/yt-transcribe/archive/refs/heads/main.tar.gz | tar xz && mv yt-transcribe-main yt-transcribe
  ```

## 1. Unraid

### 1.1 API-kulcs a llama-swapnak

1. Settings → Management Access → **API Keys** → Create.
2. Név: `llama-swap`. Jogosultság: csak **Docker**, olvasás és módosítás (`READ_ANY`, `UPDATE_ANY`). Admin szerepet ne kapjon.
3. A kulcsot másold ki, a 2.2 lépésben kell.

### 1.2 llama.cpp konténer (llama-server router módban)

A meglévő sablont használjuk (Docker fül → `llama.cpp` → Edit):

| Mező | Érték |
| --- | --- |
| Repository | `ghcr.io/ggml-org/llama.cpp:server-cuda` |
| Network Type | Host |
| Extra Parameters | `--gpus all` |
| config (Container Path `/config`) | `/mnt/user/appdata/llama.cpp/config` (itt van a `models.ini`) |
| models (Container Path `/models`) | `/mnt/user/models/` |

A **Post Arguments** mező tartalma:

```
--host 0.0.0.0 --port 8001 --models-dir /models --models-preset /config/models.ini --models-max 1 --tools all --flash-attn on --jinja --metrics --reasoning-preserve --reasoning-format deepseek --threads 15 --threads-batch 16
```

**Fontos:** a `--no-models-autoload` kapcsoló ne legyen benne. A router magától tölti be a kért presetet.

A Docker fülön a konténer **automatikus indítása legyen kikapcsolva**: ezentúl a llama-swap indítja.

### 1.3 GPU-worker konténer

Ha még fut a régi, egyben lévő `yt-transcribe` konténer, töröld: `docker rm -f yt-transcribe`. A letöltött modellek a `models` mappában maradnak, a worker újra felhasználja őket.

```bash
mkdir -p /mnt/my_new_cache/appdata/yt-transcribe
cp -r /root/src/yt-transcribe/gpu-worker /mnt/my_new_cache/appdata/yt-transcribe/
cd /mnt/my_new_cache/appdata/yt-transcribe
docker build -t yt-transcribe-gpu ./gpu-worker

docker create --name yt-transcribe-gpu --gpus all \
  -p 8766:8766 \
  -v /mnt/my_new_cache/appdata/yt-transcribe/models:/models \
  -v /mnt/my_new_cache/appdata/yt-transcribe/worker-data:/data \
  -e HF_TOKEN=hf_IDE_A_TOKENED \
  yt-transcribe-gpu
```

A `docker create` csak létrehozza a konténert, nem indítja el. Automatikus indítást nem kap, a llama-swap indítja.

A worker további beállításai (`-e NÉV=érték` a `docker create`-ben):

| Változó | Alapérték | Mire való |
| --- | --- | --- |
| `HF_TOKEN` | – | Hugging Face token a beszélőfelismeréshez |
| `DEVICE` | `auto` | `auto`: GPU, ha látható, különben CPU. `cpu`: mindig CPU |
| `WHISPER_MODELS` | `large-v3,large-v3-turbo` | A worker által elfogadott modellek |
| `COMPUTE_TYPE` | automatikus | CPU-n `int8`, GPU-n `float16` |
| `BATCH_SIZE` | automatikus | CPU-n 4, GPU-n 8 |
| `CPU_THREADS` | fizikai magok | CPU-s futásnál a szálak száma |
| `DIARIZE_MODEL` | WhisperX alapérték | Másik pyannote modell neve |
| `ASR_CACHE_DAYS` | `14` | Ennyi napig őrzi a kész átírást (a beszélőfelismerés előtti állapotot) |
| `MAX_AUDIO_MB` | `2048` | A fogadott hangfájl legnagyobb mérete |

**Ellenőrzés:** a Docker fülön megjelenik a `llama.cpp` és a `yt-transcribe-gpu` konténer, mindkettő leállítva, automatikus indítás nélkül.

## 2. Proxmox: az `ai-router` LXC

### 2.1 Az LXC létrehozása

Debian 12 sablon, privilégium nélküli konténer: 2 vCPU, 4 GB RAM, 32 GB lemez, fix IP-cím, induljon a Proxmoxszal együtt. A Proxmox felületén vagy parancssorból:

```bash
pveam update
pveam available | grep debian-12-standard        # a pontos fájlnév
pveam download local debian-12-standard_<VERZIÓ>_amd64.tar.zst

pct create 120 local:vztmpl/debian-12-standard_<VERZIÓ>_amd64.tar.zst \
  --hostname ai-router --cores 2 --memory 4096 --swap 1024 \
  --rootfs local-lvm:32 \
  --net0 name=eth0,bridge=vmbr0,ip=AI-ROUTER-IP/24,gw=192.168.1.1 \
  --unprivileged 1 --features nesting=1 --onboot 1 --start 1
pct enter 120
```

A konténerben:

```bash
apt update && apt full-upgrade -y
apt install -y curl jq ca-certificates git python3-venv openssl ffmpeg unzip rsync
git clone https://github.com/kzkz22/yt-transcribe /root/src/yt-transcribe
```

### 2.2 llama-swap

```bash
useradd --system --home /opt/llama-swap --shell /usr/sbin/nologin llamaswap
mkdir -p /opt/llama-swap /etc/llama-swap
cd /tmp && curl -fsSLO https://github.com/mostlygeek/llama-swap/releases/download/v262/llama-swap_262_linux_amd64.tar.gz
tar xzf llama-swap_262_linux_amd64.tar.gz -C /opt/llama-swap llama-swap

R=/root/src/yt-transcribe/deploy/llama-swap
install -m 755 $R/unraid-container.sh /opt/llama-swap/
install -m 644 $R/config.yaml /etc/llama-swap/config.yaml
install -m 600 -o llamaswap $R/unraid.env.example /etc/llama-swap/unraid.env
install -m 644 $R/llama-swap.service /etc/systemd/system/
```

Az `/etc/llama-swap/unraid.env` fájlban írd át a kulcsot (`UNRAID_API_KEY`). Ha az Unraid felülete HTTPS-t használ önaláírt tanúsítvánnyal, a `CURL_OPTS="-k"` sort is vedd ki a megjegyzésből. A `GPU_CONTAINERS` sorban a GPU-t használó konténerek neve áll; ezeket állítja le a script, mielőtt egy másikat elindít.

```bash
/opt/llama-swap/llama-swap -config /etc/llama-swap/config.yaml -validate
systemctl daemon-reload && systemctl enable --now llama-swap
```

**Ellenőrzés:**

```bash
# az Unraid API elérhető (leállítja a konténert, ha futott)
runuser -u llamaswap -- env UNRAID_ENV=/etc/llama-swap/unraid.env /opt/llama-swap/unraid-container.sh stop llama.cpp
# a teljes út: elindítja a konténert, a router betölti a presetet
curl http://localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"Qwen3.8-27B-UD-Q4_K_XL-Code","messages":[{"role":"user","content":"Szia!"}]}'
```

A llama-swap naplója: `journalctl -u llama-swap -f`. A webes felülete: `http://AI-ROUTER-IP:8080/ui`.

### 2.3 LiteLLM

```bash
useradd --system --home /opt/litellm --shell /usr/sbin/nologin litellm
mkdir -p /opt/litellm /etc/litellm
python3 -m venv /opt/litellm/venv
/opt/litellm/venv/bin/pip install 'litellm[proxy]==1.104.2'

R=/root/src/yt-transcribe/deploy/litellm
install -m 644 $R/config.yaml /etc/litellm/config.yaml
install -m 600 -o litellm $R/litellm.env.example /etc/litellm/litellm.env
install -m 644 $R/litellm.service /etc/systemd/system/
```

Az `/etc/litellm/litellm.env` fájlba írd be:
- az OpenRouter-kulcsot (`OPENROUTER_API_KEY`);
- egy mesterkulcsot (`LITELLM_MASTER_KEY`). Generálás: `echo "sk-$(openssl rand -hex 24)"`. Ezzel érik el a kliensek a LiteLLM-et.

```bash
systemctl daemon-reload && systemctl enable --now litellm
```

**Ellenőrzés:**

```bash
K=<mesterkulcs>
curl -s localhost:4000/v1/models -H "Authorization: Bearer $K"
curl -si localhost:4000/v1/chat/completions -H "Authorization: Bearer $K" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Szia!"}]}' | grep -i 'x-litellm-model-api-base'
```

Helyi válasznál a fejléc értéke `http://127.0.0.1:8080/v1`.

**A felhőre váltás próbája** (az Unraid „kikapcsolása” az LXC számára):

```bash
ip route add unreachable 192.168.1.20
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $K" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Szia!"}]}'      # "model":"qwen/qwen3.8-flash"
ip route del unreachable 192.168.1.20
```

A konténer leállítása nem jó próba, mert a llama-swap egyszerűen újraindítja.

Az `osszefoglalo` és az `osszefoglalo-helyi` név a webes felületé (yt-transcribe-webui): összefoglalóhoz és fordításhoz. Mindkettő a helyi Qwen3.8-27B Gnrl presetre mutat; az `osszefoglalo` kikapcsolt Unraid mellett a felhőre vált (nyilvános tartalomhoz), az `osszefoglalo-helyi` nem (saját felvételhez). A próbája ugyanaz, mint az `auto`-é: az `ip route add unreachable` alatt az `osszefoglalo` felhős választ ad, az `osszefoglalo-helyi` hibát.

### 2.4 yt-transcribe API

```bash
# deno: a yt-dlp ezzel oldja meg a YouTube JavaScript-ellenőrzését
curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
unzip -o /tmp/deno.zip -d /usr/local/bin && deno --version

useradd --system --home /var/lib/yt-transcribe --shell /usr/sbin/nologin yttranscribe
mkdir -p /opt/yt-transcribe /etc/yt-transcribe
cp -r /root/src/yt-transcribe/api /opt/yt-transcribe/api
python3 -m venv /opt/yt-transcribe/venv
/opt/yt-transcribe/venv/bin/pip install -r /opt/yt-transcribe/api/requirements.txt
chown -R yttranscribe: /opt/yt-transcribe/venv

R=/root/src/yt-transcribe/deploy/yt-transcribe
install -m 600 -o yttranscribe $R/api.env.example /etc/yt-transcribe/api.env
install -m 644 $R/yt-transcribe-api.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now yt-transcribe-api
```

Az `api.env`-ben az `ASSEMBLYAI_API_KEY` üresen hagyva kikapcsolja a felhős átírást. A többi beállítás a [README](../README.md) táblázatában van. Indításkor a szolgáltatás frissíti a yt-dlp-t; az adatai a `/var/lib/yt-transcribe` mappába kerülnek (ezt a systemd hozza létre).

**Ellenőrzés:**

```bash
curl -s http://localhost:8765/health
python3 /root/src/yt-transcribe/hermes-skill/yt-transcribe/scripts/yt_transcribe.py run \
  "https://www.youtube.com/watch?v=..." --server http://localhost:8765 --language hu --out-dir /tmp/ytt
```

A `/health` nem szól a workernek, mert az a llama-swapon keresztül elvenné a GPU-t az LLM-től. A próba-átírás közben a `journalctl -u llama-swap -f` mutatja a cserét: a llama.cpp leáll, a `yt-transcribe-gpu` elindul.

## 3. Kliensek

### 3.1 Hermes

1. A modell beállítása:
   ```
   hermes model
   ```
   Válaszd a **Custom endpoint** lehetőséget, és add meg:
   - **Base URL:** `http://AI-ROUTER-IP:4000/v1`
   - **API key:** a LiteLLM mesterkulcsa
   - **Modell:** például `Qwen3.8-27B-UD-Q4_K_XL-Code`, vagy `auto`, ha kikapcsolt Unraid mellett a felhőre váltson.
2. A yt-transcribe skill telepítése: másold a repó `hermes-skill/yt-transcribe` mappáját a Hermes `skills/media` mappájába:
   - Windows: `%LOCALAPPDATA%\hermes\skills\media\yt-transcribe`
   - Linux/macOS: `~/.hermes/skills/media/yt-transcribe`
3. A skill beállításai (a kulcsban a `skills` után `config` is kell):
   ```
   hermes config set skills.config.yt_transcribe.url http://AI-ROUTER-IP:8765
   hermes config set skills.config.yt_transcribe.out_dir ~/yt-transcripts
   ```

**Ellenőrzés:** a `hermes config show` kimenetében a „Skill Settings” részben megjelenik a két érték; a `/yt-transcribe <URL>` parancs átiratot és összefoglalót ad.

### 3.2 OpenCode

1. A `deploy/clients/opencode.json` fájlban cseréld ki az `AI-ROUTER-IP`-t. Ha már van `opencode.json`-od, a `provider.otthon` részt másold bele a meglévő mellé.
2. A kulcsot környezeti változóban kapja meg. Windows-on (utána nyiss új terminált):
   ```
   setx LITELLM_API_KEY "sk-..."
   ```
3. Az OpenCode-ban a `/models` paranccsal választhatsz. A modellek `otthon/…` néven jelennek meg.

### 3.3 VS Code

A beállítás a használt bővítménytől függ (Copilot saját modell, Continue, Cline, Roo Code). Mindegyiknél ugyanaz a három adat kell: a Base URL (`http://AI-ROUTER-IP:4000/v1`), a mesterkulcs és a modellnév.

**Minden kliensnél:** a régi közvetlen címet (`192.168.1.20:8001`) sehol ne hagyd meg. A sorba állítás csak akkor működik, ha minden kérés a LiteLLM-en és a llama-swapon keresztül megy.

## Ha valami nem működik

| Tünet | Hol nézd | Szokásos ok |
| --- | --- | --- |
| Helyi modellnévre azonnal hiba | `journalctl -u llama-swap -n 50` | Az Unraid ki van kapcsolva, rossz az API-kulcs, vagy a konténer neve nem egyezik a `config.yaml`-ban és az `unraid.env`-ben megadottal |
| `upstream command exited prematurely` | ugyanott, a `[unraid-container]` sorok | Az Unraid API hibát adott; a sor kiírja az okát |
| A kliens nem látja a modelleket | `curl localhost:4000/v1/models -H "Authorization: Bearer $K"` | Rossz mesterkulcs vagy Base URL (a végén `/v1` kell) |
| `The GPU worker could not be reached` | `journalctl -u llama-swap`, Unraid Docker fül | A `yt-transcribe-gpu` konténer hiányzik, vagy nincs benne a `GPU_CONTAINERS`-ben |
| `Speaker separation needs a Hugging Face token` | a worker `docker create` parancsa | Hiányzik a `HF_TOKEN`; a konténert újra kell létrehozni (lásd frissítés) |
| `yt-dlp could not …` | `journalctl -u yt-transcribe-api` | Régi yt-dlp: `systemctl restart yt-transcribe-api` frissíti |
