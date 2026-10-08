# yt-transcribe

Videóból vagy hangfájlból felirat beszélőcímkékkel (Szereplő 1, Szereplő 2, …), plusz Hermes skill, ami ebből összefoglalót készít.

Három része van:

- `api/` – a szolgáltatás HTTP API-ja (8765-ös port). A mindig futó Proxmox LXC-ben fut, GPU nélkül. Letölti a hangot (yt-dlp), 16 kHz-es mono FLAC-ká alakítja (ffmpeg), kezeli a feladatsort, a feltöltött fájlokat és a kimeneti fájlokat. Kétféleképpen ír át:
  - **helyi mód:** a hangot átküldi a GPU-workernek az Unraidra;
  - **felhős mód:** az AssemblyAI szolgáltatásával; ehhez az Unraidnak nem kell futnia.
- `gpu-worker/` – Docker-konténer az Unraidon. Whisper (WhisperX-en keresztül), szóra pontos időzítés és beszélőfelismerés (pyannote) az RTX 3090-en. A llama-swap indítja és állítja le, ahogy a GPU-t kiosztja.
- `hermes-skill/yt-transcribe/` – Hermes skill. Meghívja az API-t, megvárja az eredményt, beolvassa az átiratot, és az LLM-mel összefoglalja.

A forrás lehet videó-URL (YouTube és amit a yt-dlp még ismer) vagy helyi hang- és videófájl. Az átiratot nem az LLM készíti, így az összefoglaláshoz bármelyik modelled jó.

## Hogyan osztozik a GPU-n az LLM-mel

A GPU-t a Proxmoxon futó llama-swap osztja ki, mindig egy programnak. Egy helyi átírás egyetlen HTTP-kérés az API-tól a llama-swapon át a workerhez:

1. A llama-swap megvárja, míg az LLM befejezi a futó kéréseit, leállítja a llama-server konténert, és elindítja a GPU-workert.
2. A worker átír, és közben folyamatosan jelenti, hol tart.
3. Amíg a kérés nyitva van, az LLM-kérések sorban várnak. A következő LLM-kérésre a llama-swap leállítja a workert, és visszaindítja a llama-servert.

A szolgáltatás tehát maga nem ürít és nem tölt vissza modellt.

## 1. Hugging Face token (a beszélőfelismeréshez)

A pyannote modell ingyenes, de regisztrációhoz kötött:

1. Lépj be a huggingface.co-ra, és fogadd el a feltételeket itt: `pyannote/speaker-diarization-community-1`.
2. Készíts egy *read* tokent: huggingface.co/settings/tokens.

Token nélkül a worker csak beszélőcímkék nélküli átiratot ad (`--no-diarize`).

## 2. GPU-worker az Unraidon

Ha még fut a régi, egyben lévő `yt-transcribe` konténer, töröld: `docker rm -f yt-transcribe`. A letöltött modellek a `models` mappában maradnak, a worker újra felhasználja őket.

Másold a `gpu-worker/` mappát a szerverre, például ide: `/mnt/my_new_cache/appdata/yt-transcribe/gpu-worker`, majd:

```bash
cd /mnt/my_new_cache/appdata/yt-transcribe
docker build -t yt-transcribe-gpu ./gpu-worker

docker create --name yt-transcribe-gpu --gpus all \
  -p 8766:8766 \
  -v /mnt/my_new_cache/appdata/yt-transcribe/models:/models \
  -v /mnt/my_new_cache/appdata/yt-transcribe/worker-data:/data \
  -e HF_TOKEN=hf_IDE_A_TOKENED \
  yt-transcribe-gpu
```

A `docker create` csak létrehozza a konténert, nem indítja el, és automatikus újraindítást sem kap: indítani a llama-swap fogja. A Docker fülön az automatikus indítás maradjon kikapcsolva.

A worker beállításai (környezeti változók):

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

## 3. llama-swap bejegyzés (Proxmox)

1. A `deploy/llama-swap-entry.yaml` tartalmát másold a `/etc/llama-swap/config.yaml` `models:` része alá.
2. A `/etc/llama-swap/unraid.env` fájlban add hozzá a workert a GPU-s konténerekhez: `GPU_CONTAINERS="llama.cpp yt-transcribe-gpu"`.

A llama-swap a konfigurációt magától újraolvassa (`--watch-config`).

## 4. API a Proxmox LXC-ben

Ugyanabba az LXC-be kerül, mint a llama-swap és a LiteLLM:

```bash
apt install -y python3-venv ffmpeg unzip curl
# deno: a yt-dlp ezzel oldja meg a YouTube JavaScript-ellenőrzését
curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
unzip -o /tmp/deno.zip -d /usr/local/bin && deno --version

useradd --system --home /var/lib/yt-transcribe --shell /usr/sbin/nologin yttranscribe
mkdir -p /opt/yt-transcribe /etc/yt-transcribe /var/lib/yt-transcribe
# másold ide a repó api/ mappáját: /opt/yt-transcribe/api
python3 -m venv /opt/yt-transcribe/venv
/opt/yt-transcribe/venv/bin/pip install -r /opt/yt-transcribe/api/requirements.txt
chown -R yttranscribe: /opt/yt-transcribe/venv /var/lib/yt-transcribe

cp deploy/api.env.example /etc/yt-transcribe/api.env      # töltsd ki
chown yttranscribe: /etc/yt-transcribe/api.env && chmod 600 /etc/yt-transcribe/api.env
cp deploy/yt-transcribe-api.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now yt-transcribe-api
```

Indításkor a szolgáltatás frissíti a yt-dlp-t. Ha egy YouTube-videó letöltése hibát ad, először indítsd újra: `systemctl restart yt-transcribe-api`.

Ellenőrzés: `curl http://AI-ROUTER-IP:8765/health`. Ez nem szól a workernek, mert az a llama-swapon keresztül elvenné a GPU-t az LLM-től.

### Felhős mód (AssemblyAI)

Felhős módban az API letölti a hangot, kis méretű FLAC-fájllá alakítja, feltölti az AssemblyAI-hoz, és onnan kapja vissza az átiratot a beszélőcímkékkel. A GPU-hoz nem nyúl, így kikapcsolt Unraid mellett is működik.

Bekapcsolásához regisztrálj az assemblyai.com-on, és írd be a kulcsot az `api.env`-be (`ASSEMBLYAI_API_KEY`).

- **Alapból nem ezt használja.** Felhőbe csak akkor megy egy munka, ha kéred (`--mode cloud`), vagy ha a `DEFAULT_MODE`-ot átállítod.
- **Nincs csendes átváltás.** Ha a felhős munka nem sikerül, hibát kapsz. Helyi munka sem megy magától a felhőbe, akkor sem, ha az Unraid ki van kapcsolva.
- **Modellek:** `universal-2` (99 nyelv, a magyar is), `universal-3.5-pro` (pontosabb, de magyarul nem tud), `auto` (alapérték). Az `auto` a 3.5 Prót választja, ha a megadott nyelvet támogatja, különben a Universal-2-t; automatikus nyelvfelismerésnél is a Universal-2-t.
- **Havi órakeret:** a `CLOUD_MONTHLY_HOURS` (alapból 20 óra) fölött nem indít új felhős munkát.
- **Költségbecslés:** az eredményben szerepel a becsült ár a 2026. októberi árlista alapján (Universal-2: 0,15 USD/óra, 3.5 Pro: 0,21 USD/óra, beszélőfelismerés: +0,02 USD/óra).
- **Törlés:** az eredmény letöltése után törli az átiratot és a feltöltött hangot az AssemblyAI-nál (`CLOUD_DELETE_AFTER=0` kikapcsolja).
- **EU-s adatkezelés:** `ASSEMBLYAI_BASE_URL=https://api.eu.assemblyai.com`.

### Az API beállításai (`/etc/yt-transcribe/api.env`)

| Változó | Alapérték | Mire való |
| --- | --- | --- |
| `DATA_DIR` | `/var/lib/yt-transcribe` | Átiratok, feltöltött fájlok, felhős keret |
| `WORKER_URL` | `http://127.0.0.1:8080/upstream/yt-transcribe-gpu` | A GPU-worker a llama-swapon keresztül |
| `WORKER_TIMEOUT_MIN` | `30` | Egy helyi munka leghosszabb ideje, a GPU-ra várással együtt; hosszú hangnál a hang hosszának négyszereséig nő. Ha lejár, a munka leáll, és a worker elengedi a GPU-t |
| `DEFAULT_MODE` | `local` | Az alapértelmezett mód: `local` vagy `cloud` |
| `WHISPER_MODEL` | `large-v3` | A helyi mód alapmodellje |
| `LOCAL_MODELS` | `large-v3,large-v3-turbo` | A kérésben választható helyi modellek |
| `ASSEMBLYAI_API_KEY` | – | A felhős mód kulcsa; nélküle a felhős mód nem érhető el |
| `ASSEMBLYAI_BASE_URL` | `https://api.assemblyai.com` | EU-s végpont: `https://api.eu.assemblyai.com` |
| `CLOUD_MODEL` | `auto` | A felhős mód alapmodellje |
| `CLOUD_MONTHLY_HOURS` | `20` | Havi órakeret a felhős módra; `0` = nincs korlát |
| `CLOUD_DELETE_AFTER` | `1` | Törlés a szolgáltatónál az eredmény letöltése után |
| `MAX_UPLOAD_GB` | `8` | Feltölthető helyi fájl legnagyobb mérete |
| `UPLOAD_TTL_DAYS` | `7` | Ennyi nap után törli a feltöltött fájlokat |
| `SPEAKER_LABEL` | nyelv szerint | Magyar videónál `Szereplő`, egyébként `Speaker`; itt felülírható |
| `MAX_DURATION_MIN` | `360` | Ennél hosszabb videót nem fogad el |
| `YTDLP_COOKIES` | – | cookies.txt útvonala, ha a YouTube bejelentkezést kér |

## 5. Hermes skill telepítése

Másold a `hermes-skill/yt-transcribe` mappát a Hermes `skills/media` mappájába. Ez a `config.yaml` mellett van:

- Linux/macOS: `~/.hermes/skills/media/yt-transcribe`
- Windows: `%LOCALAPPDATA%\hermes\skills\media\yt-transcribe`

Utána állítsd be az API címét. A kulcsban a `skills` után `config` is kell:

```bash
hermes config set skills.config.yt_transcribe.url http://AI-ROUTER-IP:8765
hermes config set skills.config.yt_transcribe.out_dir ~/yt-transcripts
```

Ellenőrzés: a `hermes config show` kimenetében a „Skill Settings" résznél kell megjelenniük.

## 6. Használat

Hermesben:

```
/yt-transcribe https://www.youtube.com/watch?v=... készíts feliratot és összefoglalót, ketten beszélgetnek
/yt-transcribe "D:\felvételek\interjú.mp4" magyar nyelvű, felirat kell hozzá
/yt-transcribe https://www.youtube.com/watch?v=... felhőben írd át, angol nyelvű
```

A skill csak akkor használ felhős módot vagy a nem alapértelmezett modellt, ha a kérésben ezt megmondod.

Hermes nélkül, közvetlenül:

```bash
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py run "URL" --server http://AI-ROUTER-IP:8765 --speakers 2
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py run "interjú.mp4" --server http://AI-ROUTER-IP:8765 --mode cloud --language hu
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py wait JOB_ID --server http://AI-ROUTER-IP:8765
```

A `run` kapcsolói:

| Kapcsoló | Értékek | Alapérték |
| --- | --- | --- |
| `--mode` | `local`, `cloud` | az API `DEFAULT_MODE` értéke |
| `--model` | helyi: `large-v3`, `large-v3-turbo`; felhős: `auto`, `universal-2`, `universal-3.5-pro` | módonként az alapmodell |
| `--language` | `hu`, `en`, … | automatikus felismerés |
| `--speakers` | a beszélők pontos száma | automatikus |
| `--min-speakers`, `--max-speakers` | tartomány | – |
| `--no-diarize` | beszélőcímkék nélkül | címkékkel |
| `--force` | újraszámolás a gyorsítótár helyett | – |

Helyi fájlt a szkript feltölt az API-nak. Ugyanazt a fájlt másodszor már nem tölti fel újra.

A kimenet videónként három fájl:

- `transcript.txt` – olvasható átirat: `[00:01:23] Szereplő 1: …`
- `transcript.srt` – feliratfájl, soronként `[Szereplő 1] …`
- `result.json` – minden adat gépi feldolgozáshoz, benne a mód, a modell és felhős munkánál a becsült költség

Ugyanazt a videót ugyanazokkal a beállításokkal újra kérve az eredmény azonnal jön a gyorsítótárból. A mód és a modell is része a kulcsnak, így a helyi és a felhős átirat megmarad egymás mellett (`--force`: újraszámolás). Helyi módban a worker az átírást (a lassú részt) külön is elmenti, így ha egy munka később megszakad, vagy más beszélőszámmal kéred újra, csak a beszélőfelismerés fut le ismét.

A mentett fájlok a kliensen videónként és azon belül módonként, modellenként külön mappába kerülnek (például `youtube_ABC/local-large-v3/` és `youtube_ABC/cloud-universal-2/`).

## Jó tudni

- Ha tudod, hányan beszélnek, add meg (`--speakers 2`), ez érezhetően javítja a szétválasztást.
- A beszélőfelismerés hangokat különböztet meg, neveket nem tud. Egymás szavába vágásnál, hasonló hangoknál és egy-két szavas közbeszólásoknál téved a leggyakrabban.
- A nyelvet a videó elejéből ismeri fel. Ha téved, add meg: `--language hu` vagy `--language en`.
- Átírás közben az LLM nem válaszol, a kérések sorban várnak, és utána a modell újratöltődik. Ilyenkor a prompt cache elvész, a beszélgetést újra fel kell dolgoznia.
- A Hermes egy terminálhívása legfeljebb 600 másodpercig várhat. Ha egy nagyon hosszú videó ennél tovább tart, a skill `wait` paranccsal folytatja; közben ne kérj mást az LLM-től.
- Egyszerre egy videót dolgoz fel, a többi sorban áll. Minden munka külön folyamatban fut az API-ban és a workerben is, így egy összeomlás csak azt a munkát állítja le.
- A szolgáltatásban nincs hitelesítés, csak a helyi hálózaton használd, kifelé ne nyisd meg a portot.

## Fejlesztés és tesztek

A tesztek a valódi API-t és a valódi workert futtatják, a nehéz és külső részeket (Whisper, pyannote, yt-dlp, AssemblyAI) helyettesítőkkel váltják ki, így GPU és internet nélkül futnak:

```bash
pip install -r tests/requirements.txt   # és kell az ffmpeg/ffprobe
python -m pytest -q tests
```

A fejlesztéshez szükséges háttér (architektúra, meghozott döntések, mi van élesben kipróbálva) a `CLAUDE.md`-ben van.
