# yt-transcribe

Videóból vagy hangfájlból felirat beszélőcímkékkel (Szereplő 1, Szereplő 2, …), plusz Hermes skill, ami ebből összefoglalót készít.

Két része van:

- `service/` – Docker konténer a szerverre, HTTP API-val a 8765-ös porton. Kétféleképpen tud átírni:
  - **helyi mód:** Whisper (WhisperX-en keresztül), szóra pontos időzítés, beszélőfelismerés (pyannote) a saját gépeden;
  - **felhős mód:** az AssemblyAI szolgáltatásával, a GPU érintése nélkül.
- `hermes-skill/yt-transcribe/` – Hermes skill. Meghívja a szolgáltatást, megvárja az eredményt, beolvassa az átiratot, és az LLM-mel összefoglalja.

A forrás lehet videó-URL (YouTube és amit a yt-dlp még ismer) vagy helyi hang- és videófájl. Az átiratot nem az LLM készíti, így az összefoglaláshoz bármelyik modelled jó.

## 1. Hugging Face token (a beszélőfelismeréshez)

A pyannote modell ingyenes, de regisztrációhoz kötött:

1. Lépj be a huggingface.co-ra, és fogadd el a feltételeket itt: `pyannote/speaker-diarization-community-1`.
2. Készíts egy *read* tokent: huggingface.co/settings/tokens.

Token nélkül a szolgáltatás működik, de csak beszélőcímkék nélküli átiratot ad (`--no-diarize`).

## 2. Konténer építése és indítása (Unraid)

Másold a `service/` mappát a szerverre, például ide: `/mnt/my_new_cache/appdata/yt-transcribe/service`.

```bash
cd /mnt/my_new_cache/appdata/yt-transcribe
docker build -t yt-transcribe ./service

docker run -d --name yt-transcribe --restart unless-stopped \
  -p 8765:8765 \
  -v /mnt/my_new_cache/appdata/yt-transcribe/models:/models \
  -v /mnt/my_new_cache/appdata/yt-transcribe/data:/data \
  -e HF_TOKEN=hf_IDE_A_TOKENED \
  yt-transcribe
```

Így, GPU nélkül indítva CPU-n fut: nem vesz el VRAM-ot az LLM-től, de lassú (egy 14 perces videó 25 percnél is tovább tartott). Az image nagy (a függőségek kb. 7,6 GB-ot foglalnak), az első futáskor pedig a modellek is letöltődnek a `models` mappába (több GB).

Ellenőrzés: `curl http://SZERVER_IP:8765/health`

### GPU-n futtatva, az LLM-mel megosztva

Egy 24 GB-os kártyán a nagy kontextusú LLM és a beszédmodellek nem férnek el egyszerre, ezért a szolgáltatás felváltva használja a GPU-t az LLM-mel:

1. Munka előtt megnézi, van-e elég szabad VRAM.
2. Ha nincs, megkéri a llama-servert (router mód), hogy ürítse ki a betöltött modellt.
3. Lefuttatja az átírást a GPU-n, majd a munkafolyamat kilép, így minden VRAM felszabadul.
4. Visszatölteti a llama-serverrel ugyanazt a modellt, megvárja, míg betöltődik, és csak utána jelzi késznek a munkát.

Ehhez a `docker run` parancsba még ez kell (a cím a llama-server címe és portja):

```bash
  --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all \
  -e LLAMA_SERVER_URL=http://SZERVER_IP:PORT \
```

Amivel számolni kell:

- Minden átírás után az LLM újratöltődik (ha több videó áll sorban, csak az utolsó után), és a beszélgetés addigi szövegét újra fel kell dolgoznia, mert a prompt cache a modell kiürítésével elvész.
- Amíg az átírás fut, ne használd az LLM-et más kliensből: a kérésed visszatöltené a modellt, és összeakadnának a VRAM-on. Ilyenkor a szolgáltatás a hátralévő részt CPU-n fejezi be, és ezt jelzi az eredményben.
- A Hermes egy terminálhívása legfeljebb 600 másodpercig várhat. Ha egy nagyon hosszú videó ennél tovább tart, a Hermes közben megszólítja az LLM-et, és előáll az előző pont. Ilyenkor emeld meg a Hermes `TERMINAL_MAX_FOREGROUND_TIMEOUT` környezeti változóját.
- Ez csak llama-server router móddal működik. Más motornál (például Strata) a szolgáltatás nem tudja kiüríteni a modellt, ezért CPU-n fut, és jelzi az okát.

### Felhős mód (AssemblyAI)

Felhős módban a szolgáltatás letölti a hangot, kis méretű FLAC-fájllá alakítja, feltölti az AssemblyAI-hoz, és onnan kapja vissza az átiratot a beszélőcímkékkel. A GPU-hoz nem nyúl, a Qwen végig betöltve marad.

Bekapcsolásához regisztrálj az assemblyai.com-on, és add meg az API-kulcsot a `docker run` parancsban:

```bash
  -e ASSEMBLYAI_API_KEY=IDE_A_KULCS \
```

Amit tudni kell róla:

- **Alapból nem ezt használja.** Az alapértelmezett mód a helyi; felhőbe csak akkor megy egy munka, ha kéred (`--mode cloud`), vagy ha a `DEFAULT_MODE`-ot átállítod.
- **Nincs csendes átváltás.** Ha a felhős munka nem sikerül, hibát kapsz, nem fut le helyben helyette. Helyi munka pedig soha nem megy magától a felhőbe.
- **Modellek:** `universal-2` (99 nyelv, a magyar is), `universal-3.5-pro` (pontosabb, de magyarul nem tud), `auto` (alapérték). Az `auto` a 3.5 Prót választja, ha a megadott nyelvet támogatja, különben a Universal-2-t; automatikus nyelvfelismerésnél is a Universal-2-t.
- **Havi órakeret:** a `CLOUD_MONTHLY_HOURS` (alapból 20 óra) fölött a szolgáltatás nem indít új felhős munkát. Ez biztonsági háló, a tényleges fogyasztást a szolgáltató felületén látod.
- **Költségbecslés:** az eredményben szerepel a becsült ár a 2026. októberi árlista alapján (Universal-2: 0,15 USD/óra, 3.5 Pro: 0,21 USD/óra, beszélőfelismerés: +0,02 USD/óra).
- **Törlés:** az eredmény letöltése után a szolgáltatás törli az átiratot és a feltöltött hangot az AssemblyAI-nál (`CLOUD_DELETE_AFTER=0` kikapcsolja).
- **EU-s adatkezelés:** az `ASSEMBLYAI_BASE_URL=https://api.eu.assemblyai.com` beállítással az EU-s végpontot használja.

### Beállítások (környezeti változók)

| Változó | Alapérték | Mire való |
| --- | --- | --- |
| `HF_TOKEN` | – | Hugging Face token a beszélőfelismeréshez |
| `DEVICE` | `auto` | `auto`: GPU, ha látható és felszabadítható, különben CPU. `cpu`: mindig CPU |
| `LLAMA_SERVER_URL` | – | A llama-server (router mód) címe; ezen keresztül kéri a modell kiürítését |
| `LLAMA_SERVER_API_KEY` | – | Ha a llama-server API-kulcsot kér |
| `MIN_FREE_VRAM_MB` | `7000` | Ennyi szabad VRAM kell ahhoz, hogy a GPU-t használja |
| `DEFAULT_MODE` | `local` | Az alapértelmezett mód: `local` vagy `cloud` |
| `WHISPER_MODEL` | `large-v3` | A helyi mód alapmodellje |
| `LOCAL_MODELS` | `large-v3,large-v3-turbo` | A kérésben választható helyi modellek |
| `ASSEMBLYAI_API_KEY` | – | A felhős mód kulcsa; nélküle a felhős mód nem érhető el |
| `ASSEMBLYAI_BASE_URL` | `https://api.assemblyai.com` | EU-s végpont: `https://api.eu.assemblyai.com` |
| `CLOUD_MODEL` | `auto` | A felhős mód alapmodellje |
| `CLOUD_MONTHLY_HOURS` | `20` | Havi órakeret a felhős módra; `0` = nincs korlát |
| `CLOUD_DELETE_AFTER` | `1` | Törlés a szolgáltatónál az eredmény letöltése után |
| `MAX_UPLOAD_GB` | `8` | Feltölthető helyi fájl legnagyobb mérete |
| `UPLOAD_TTL_DAYS` | `7` | Ennyi nap után törli a feltöltött fájlokat a szerverről |
| `COMPUTE_TYPE` | automatikus | CPU-n `int8`, GPU-n `float16` |
| `BATCH_SIZE` | automatikus | CPU-n 4, GPU-n 8 |
| `CPU_THREADS` | fizikai magok | CPU-s futásnál a szálak száma |
| `SPEAKER_LABEL` | nyelv szerint | Magyar videónál `Szereplő`, egyébként `Speaker`; itt felülírható |
| `MAX_DURATION_MIN` | `360` | Ennél hosszabb videót nem fogad el |
| `YTDLP_AUTO_UPDATE` | `1` | Induláskor frissíti a yt-dlp-t |
| `YTDLP_COOKIES` | – | cookies.txt útvonala, ha a YouTube bejelentkezést kér |

## 3. Hermes skill telepítése

Másold a `hermes-skill/yt-transcribe` mappát a Hermes `skills/media` mappájába. Ez a `config.yaml` mellett van:

- Linux/macOS: `~/.hermes/skills/media/yt-transcribe`
- Windows: `%LOCALAPPDATA%\hermes\skills\media\yt-transcribe`

Utána állítsd be a szolgáltatás címét. A kulcsban a `skills` után `config` is kell:

```bash
hermes config set skills.config.yt_transcribe.url http://SZERVER_IP:8765
hermes config set skills.config.yt_transcribe.out_dir ~/yt-transcripts
```

Ellenőrzés: a `hermes config show` kimenetében a „Skill Settings" résznél kell megjelenniük.

Ha a Hermes terminálja Docker-sandboxban fut, a `localhost` nem a szervert jelenti, ezért mindenképp a szerver IP-címét add meg.

## 4. Használat

Hermesben:

```
/yt-transcribe https://www.youtube.com/watch?v=... készíts feliratot és összefoglalót, ketten beszélgetnek
/yt-transcribe "D:\felvételek\interjú.mp4" magyar nyelvű, felirat kell hozzá
/yt-transcribe https://www.youtube.com/watch?v=... felhőben írd át, angol nyelvű
```

A skill csak akkor használ felhős módot vagy a nem alapértelmezett modellt, ha a kérésben ezt megmondod.

Hermes nélkül, közvetlenül:

```bash
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py run "URL" --server http://SZERVER_IP:8765 --speakers 2
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py run "interjú.mp4" --server http://SZERVER_IP:8765 --mode cloud --language hu
python hermes-skill/yt-transcribe/scripts/yt_transcribe.py wait JOB_ID --server http://SZERVER_IP:8765
```

A `run` kapcsolói:

| Kapcsoló | Értékek | Alapérték |
| --- | --- | --- |
| `--mode` | `local`, `cloud` | a konténer `DEFAULT_MODE` értéke |
| `--model` | helyi: `large-v3`, `large-v3-turbo`; felhős: `auto`, `universal-2`, `universal-3.5-pro` | módonként a konténer alapmodellje |
| `--language` | `hu`, `en`, … | automatikus felismerés |
| `--speakers` | a beszélők pontos száma | automatikus |
| `--min-speakers`, `--max-speakers` | tartomány | – |
| `--no-diarize` | beszélőcímkék nélkül | címkékkel |
| `--force` | újraszámolás a gyorsítótár helyett | – |

Helyi fájlt a szkript feltölt a szolgáltatásnak. Ugyanazt a fájlt másodszor már nem tölti fel újra.

A kimenet videónként három fájl:

- `transcript.txt` – olvasható átirat: `[00:01:23] Szereplő 1: …`
- `transcript.srt` – feliratfájl, soronként `[Szereplő 1] …`
- `result.json` – minden adat gépi feldolgozáshoz, benne a mód, a modell és felhős munkánál a becsült költség

Ugyanazt a videót ugyanazokkal a beállításokkal újra kérve az eredmény azonnal jön a gyorsítótárból. A mód és a modell is része a kulcsnak, tehát ugyanaz a videó helyben és felhőben is átíratható, és a két eredmény megmarad egymás mellett. Újraszámolás: `--force`. Helyi módban az átírás (a lassú rész) külön is elmentődik, így ha egy munka később megszakad, vagy más beszélőszámmal kéred újra, csak a beszélőfelismerés fut le ismét.

A mentett fájlok a kliensen videónként és azon belül módonként, modellenként külön mappába kerülnek (például `youtube_ABC/local-large-v3/` és `youtube_ABC/cloud-universal-2/`), így a helyi és a felhős átirat közvetlenül összevethető.

## Jó tudni

- Ha tudod, hányan beszélnek, add meg (`--speakers 2`), ez érezhetően javítja a szétválasztást.
- A beszélőfelismerés hangokat különböztet meg, neveket nem tud. Egymás szavába vágásnál, hasonló hangoknál és egy-két szavas közbeszólásoknál téved a leggyakrabban.
- A nyelvet a videó elejéből ismeri fel. Ha téved, add meg: `--language hu` vagy `--language en`.
- A felhős feliratfájl tagolása kicsit más, mint a helyié: ott a szolgáltató szavaiból, mondatvégi írásjelek mentén készülnek a sorok.
- Egyszerre egy videót dolgoz fel, a többi sorban áll. Minden munka külön folyamatban fut, így ha elfogy a memória, csak az a munka áll le, a szolgáltatás nem.
- A szolgáltatásban nincs hitelesítés, csak a helyi hálózaton használd, kifelé ne nyisd meg a portot.

## Fejlesztés és tesztek

A tesztek a nehéz részeket (Whisper, pyannote, yt-dlp, llama-server, AssemblyAI) helyettesítőkkel váltják ki, így GPU és internet nélkül futnak:

```bash
pip install -r tests/requirements.txt   # és kell az ffmpeg/ffprobe
python -m pytest -q tests
```

A fejlesztéshez szükséges háttér (architektúra, meghozott döntések, mi van élesben kipróbálva) a `CLAUDE.md`-ben van.

