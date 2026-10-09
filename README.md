# yt-transcribe

Videóból vagy hangfájlból felirat beszélőcímkékkel (Szereplő 1, Szereplő 2, …), plusz Hermes skill, ami ebből összefoglalót készít.

Három része van:

- `api/` – a szolgáltatás HTTP API-ja (8765-ös port). A mindig futó Proxmox LXC-ben fut, GPU nélkül. Letölti a hangot (yt-dlp), 16 kHz-es mono FLAC-ká alakítja (ffmpeg), kezeli a feladatsort, a feltöltött fájlokat és a kimeneti fájlokat. Kétféleképpen ír át:
  - **helyi mód:** a hangot átküldi a GPU-workernek az Unraidra;
  - **felhős mód:** az AssemblyAI szolgáltatásával; ehhez az Unraidnak nem kell futnia.
- `gpu-worker/` – Docker-konténer az Unraidon. Whisper (WhisperX-en keresztül), szóra pontos időzítés és beszélőfelismerés (pyannote) az RTX 3090-en. A llama-swap indítja és állítja le, ahogy a GPU-t kiosztja.
- `hermes-skill/yt-transcribe/` – Hermes skill. Meghívja az API-t, megvárja az eredményt, beolvassa az átiratot, és az LLM-mel összefoglalja.

A forrás lehet videó-URL (YouTube és amit a yt-dlp még ismer) vagy helyi hang- és videófájl. Az átiratot nem az LLM készíti, így az összefoglaláshoz bármelyik modelled jó.

[![Architektúra](docs/architektura.svg)](docs/architektura.md)

## Hogyan osztozik a GPU-n az LLM-mel

A GPU-t a Proxmoxon futó llama-swap osztja ki, mindig egy programnak. Egy helyi átírás egyetlen HTTP-kérés az API-tól a llama-swapon át a workerhez:

1. A llama-swap megvárja, míg az LLM befejezi a futó kéréseit, leállítja a llama-server konténert, és elindítja a GPU-workert.
2. A worker átír, és közben folyamatosan jelenti, hol tart.
3. Amíg a kérés nyitva van, az LLM-kérések sorban várnak. A következő LLM-kérésre a llama-swap leállítja a workert, és visszaindítja a llama-servert.

A szolgáltatás tehát maga nem ürít és nem tölt vissza modellt.

## Telepítés és frissítés

A teljes rendszer (Unraid, Proxmox LXC a llama-swappal és a LiteLLM-mel, kliensek) leírása a `docs/` mappában van:

- [Architektúra](docs/architektura.md): az ábra, a komponensek, a kérések útja, és hogy mi miért van.
- [Telepítés lépésről lépésre](docs/telepites.md): minden komponens, ellenőrzéssel.
- [Frissítés és karbantartás](docs/frissites.md): komponensenként, visszalépéssel együtt.

A telepítéshez szükséges fájlok a `deploy/` mappában vannak:

| Mappa | Tartalom |
| --- | --- |
| `deploy/llama-swap/` | `config.yaml`, az Unraid API-s indítóscript, a kulcsfájl mintája, systemd-szolgáltatás |
| `deploy/litellm/` | `config.yaml` (modellnevek, tartalékváltás), a kulcsfájl mintája, systemd-szolgáltatás |
| `deploy/yt-transcribe/` | az API systemd-szolgáltatása és beállításainak mintája |
| `deploy/clients/` | OpenCode-beállítás |

## Felhős mód (AssemblyAI)

Felhős módban az API letölti a hangot, kis méretű FLAC-fájllá alakítja, feltölti az AssemblyAI-hoz, és onnan kapja vissza az átiratot a beszélőcímkékkel. A GPU-hoz nem nyúl, így kikapcsolt Unraid mellett is működik.

Bekapcsolásához regisztrálj az assemblyai.com-on, és írd be a kulcsot az `api.env`-be (`ASSEMBLYAI_API_KEY`).

- **Alapból nem ezt használja.** Felhőbe csak akkor megy egy munka, ha kéred (`--mode cloud`), vagy ha a `DEFAULT_MODE`-ot átállítod.
- **Nincs csendes átváltás.** Ha a felhős munka nem sikerül, hibát kapsz. Helyi munka sem megy magától a felhőbe, akkor sem, ha az Unraid ki van kapcsolva.
- **Modellek:** `universal-2` (99 nyelv, a magyar is), `universal-3.5-pro` (pontosabb, de magyarul nem tud), `auto` (alapérték). Az `auto` a 3.5 Prót választja, ha a megadott nyelvet támogatja, különben a Universal-2-t; automatikus nyelvfelismerésnél is a Universal-2-t.
- **Havi órakeret:** a `CLOUD_MONTHLY_HOURS` (alapból 20 óra) fölött nem indít új felhős munkát.
- **Költségbecslés:** az eredményben szerepel a becsült ár a 2026. októberi árlista alapján (Universal-2: 0,15 USD/óra, 3.5 Pro: 0,21 USD/óra, beszélőfelismerés: +0,02 USD/óra).
- **Törlés:** az eredmény letöltése után törli az átiratot és a feltöltött hangot az AssemblyAI-nál (`CLOUD_DELETE_AFTER=0` kikapcsolja).
- **EU-s adatkezelés:** `ASSEMBLYAI_BASE_URL=https://api.eu.assemblyai.com`.

## Az API beállításai (`/etc/yt-transcribe/api.env`)

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

## Használat

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
