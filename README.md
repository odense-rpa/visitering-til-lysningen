## Visitering til Lysningen

Automatisering der håndterer visitering af borgere til midlertidigt ophold på Lysningen i Odense Kommune.

## Hvad gør robotten?

1. **Henter opgaver** fra KMD Nexus — både bevilgede ophold og afviste sager fra organisationerne "Lysningen indflytning" og "Robot afvist Lysningen"
2. **Fylder arbejdskøen** med borgere der afventer visitering (CPR-nummer bruges som reference)
3. **Finder det seneste relevante skema** for borgeren i Nexus ("Indstilling og afgørelse til midlertidigt ophold på Lysningen" – Servicelov eller Ældrelov)
4. **Udlæser bevillingsindsatsen** fra skemaet for at bestemme den korrekte indsatstype
5. **Tilknytter borgeren** til de relevante organisationer i Nexus (Økonomi og Effekt, Lysningen, Myndighed Lysningen)
6. **Opretter indsatser** under "Ældre og sundhedsfagligt grundforløb" – herunder opstartsindsatser og bevillingsindsats
7. **Lukker afviste opgaver** i organisationen "Robot afvist Lysningen"
8. **Registrerer aktivitet** via ODK-tracking

## Forudsætninger

- Python ≥ 3.13
- [`uv`](https://docs.astral.sh/uv/) til pakkehåndtering
- Adgang til **Automation Server** (arbejdskø)
- Adgang til **KMD Nexus** (produktion)
- En **Odense SQL Server**-konto til aktivitetssporing
- En Excel-fil (`Regelsæt.xlsx`) med listen over potentielle organisationer

## Installation

```sh
uv sync
```

## Konfiguration

Kopiér `.env.example` til `.env` og udfyld følgende:

| Variabel | Beskrivelse |
|---|---|
| `EXCEL_MAPPING_PATH` | Sti til `Regelsæt.xlsx` |
| _(Automation Server-variabler)_ | Ifølge `automation-server-client`-dokumentationen |

Legitimationsoplysninger (KMD Nexus, Odense SQL Server) hentes via Automation Server Credentials og angives ikke i `.env`.

## Kørsel

```sh
# Fyld arbejdskøen med nye opgaver fra Nexus
uv run python main.py --queue

# Behandl arbejdskøen
uv run python main.py
```

### Argumenter

| Argument | Beskrivelse |
|---|---|
| `--excel-file <sti>` | Tilsidesæt stien til `Regelsæt.xlsx` |
| `--queue` | Fyld arbejdskøen og afslut (kør ingen behandling) |

## Regelopsætning (`Regelsæt.xlsx`)

Excel-filen indeholder et ark:

- **Liste** – Kolonne med organisationsnavne der potentielt skal modtage besked ved visitering til Lysningen

## Afhængigheder

| Pakke | Formål |
|---|---|
| `automation-server-client` | Arbejdskø-håndtering |
| `kmd-nexus-client` | Integration med KMD Nexus |
| `odk-tools` | Aktivitetssporing |
| `openpyxl` | Læsning af Excel-fil |

## Persondatasikkerhed

Robotten behandler personoplysninger på vegne af Odense Kommune, herunder CPR-numre tilhørende borgere der visiteres til midlertidigt ophold.

- Ingen personoplysninger må lægges i dette repository — hverken som testdata, i kode eller i kommentarer
- `input/`-mappen er ekskluderet via `.gitignore` og må aldrig committes
- Legitimationsoplysninger håndteres udelukkende via miljøvariabler (`.env`) og Automation Server Credentials

