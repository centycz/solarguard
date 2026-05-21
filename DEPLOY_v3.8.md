# SolarGuard v3.8 - Deploy + InfluxDB + Grafana setup

Nový velký feature: **dlouhodobé úložiště dat** v InfluxDB 2.x + krásné dashboardy v Grafaně.

## Co to dělá

SolarGuard každých 30s posílá kompletní stav (FVE, baterie, vířivka, počasí, spot ceny, rozhodnutí planneru, heating curve samples) do InfluxDB. Grafana to potom vykresluje s libovolným time range - hodina, den, týden, měsíc, rok.

**Co získáš:**
- 📊 **Trendy přes dny/týdny** - kdy nejvíc vyrábíš, kdy nejvíc spotřebuješ
- 🛁 **Heating curve graf** - každý ohřev jako bod, vidíš jak se model učí
- 💰 **Historie spot cen** - kdy bylo nejdráž za poslední měsíc
- 🌡 **Korelace** - ohřev vody vs teplota venku, výroba vs sluneční svit
- 🚨 **Anomálie** - "dnes vyrobili o 30% méně než průměr" → zkontroluj panel

**SolarGuard funguje i bez InfluxDB** - pokud `enabled: false` v configu, vše jede stejně jako dřív.

## Část 1: Update SolarGuard na v3.8

### 1a) Záloha + stop

```bash
sudo systemctl stop solarguard
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
tar czf solarguard-pred-v38-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/
ls -la solarguard-pred-v38-*.tar.gz
```

### 1b) Přepsat soubory

ZIP `solarguard-v3.8-FULL.zip` obsahuje **kompletní v3.8** (kód + Grafana dashboardy).

Rozbal a nahraj přes WinSCP. Změněné/nové soubory:

| Soubor | Akce |
|---|---|
| `main.py` | nahradit |
| `config.yaml` | nahradit (nebo přidat sekci `influxdb:` ručně) |
| `solarguard/web.py` | nahradit |
| `solarguard/storage/influx.py` | **NEW** |
| `grafana/dashboards/*.json` | nové (3 dashboardy) |

### 1c) Spustit jen SolarGuard (zatím bez InfluxDB)

```bash
sudo systemctl start solarguard
sudo journalctl -u solarguard -f
```

V logu bys měl vidět:
```
SolarGuard v3.8 starting (dry_run=False)
```

InfluxDB v configu je `enabled: false` → nepokouší se připojit, žádné chyby.

V dashboardu **Více → 🗄 Long-term storage** uvidíš:
> ⚪ nenakonfigurováno
> InfluxDB není zapnutá. Pro dlouhodobé grafy přes Grafanu zapni v config.yaml...

OK, SolarGuard funguje. Pojďme dál.

---

## Část 2: Instalace InfluxDB 2.x

Tři možnosti - vyber si:

### Varianta A: Native install na RPi (doporučeno, jednoduché)

```bash
# Importuj GPG klíč a přidej repo
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c
cat influxdata-archive_compat.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | sudo tee /etc/apt/sources.list.d/influxdata.list

sudo apt-get update
sudo apt-get install -y influxdb2

sudo systemctl enable influxdb
sudo systemctl start influxdb

# Zkontroluj že běží
curl -s http://localhost:8086/health
# {"name":"influxdb","message":"ready for queries and writes","status":"pass","checks":[],"version":"2.x.x","commit":"..."}
```

### Varianta B: Docker (pokud preferuješ)

```bash
mkdir -p /home/pi/influxdb-data
docker run -d \
  --name influxdb \
  --restart unless-stopped \
  -p 8086:8086 \
  -v /home/pi/influxdb-data:/var/lib/influxdb2 \
  influxdb:2.7
```

### Varianta C: Na NAS / jiném stroji

InfluxDB může běžet na NAS (Synology, QNAP) nebo jiném serveru v síti. Stačí v configu nastavit `url: "http://10.0.0.X:8086"`.

---

## Část 3: Setup InfluxDB

Otevři v prohlížeči **`http://10.0.0.X:8086`** (kde X je IP RPi nebo NAS).

### 3a) Initial setup wizard

1. **Get Started** → **Continue**
2. **Username**: `pi` (nebo cokoli)
3. **Password**: silné heslo (minimálně 8 znaků), zapamatuj si!
4. **Organization Name**: `home`
5. **Bucket Name**: `solarguard`
6. **Continue**

### 3b) Vytvoření API tokenu

1. Vlevo dole **"Load Data"** → **"API Tokens"**
2. **"Generate API Token"** → **"All Access API Token"** (pro začátek nejjednodušší)
3. **Description**: `SolarGuard writer`
4. **Save**
5. **Zkopíruj token** - je to dlouhý string začínající něčím jako `pq3O2k...`. Po zavření okna ho **už neuvidíš**! Ulož si ho hned.

### 3c) Nastavit retention policy

V tom samém **Load Data** → **Buckets** → klikni na `solarguard` → **Settings** → **Retention**:

- **Forever** = nikdy nemaže (může se časem rozjet, asi 100 MB / měsíc)
- **30 days** / **90 days** / **1 year** = doporučeno

Pro začátek navrhuju **1 year** - rok historie je super pro analýzy a 1.2 GB ti RPi unese.

### 3d) Aktualizovat config.yaml

```bash
sudo nano /home/pi/solarguard/config.yaml
```

Najdi sekci `influxdb:` a uprav:

```yaml
influxdb:
  enabled: true                                    # zapnuto!
  url: "http://localhost:8086"                     # nebo http://nas:8086
  org: "home"
  bucket: "solarguard"
  token: "pq3O2k_TVUJ_TOKEN_TADY_paste_z_kroku_3b"
  location_tag: "bojanovice"
```

### 3e) Restart SolarGuard

```bash
sudo systemctl restart solarguard
sudo journalctl -u solarguard -f | grep -i influx
```

V logu by mělo být:
```
[INFO] influx: InfluxDB connected: http://localhost:8086 bucket=solarguard
```

A v dashboardu **Více → 🗄 Long-term storage**:
> ● PŘIPOJENO
> URL: http://localhost:8086
> Zapsáno bodů: 35 (počet roste cca o 5 každých 30s)

Pokud místo toho vidíš `● ODPOJENO`, zkontroluj:
- Token v configu odpovídá tomu z UI
- `org` a `bucket` přesně shoduje
- `curl http://localhost:8086/health` z RPi vrací 200

---

## Část 4: Instalace Grafany

### 4a) Native install (doporučeno)

```bash
# Repo
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee -a /etc/apt/sources.list.d/grafana.list

sudo apt-get update
sudo apt-get install -y grafana

sudo systemctl enable grafana-server
sudo systemctl start grafana-server
```

### 4b) Docker (alternativa)

```bash
docker run -d \
  --name grafana \
  --restart unless-stopped \
  -p 3000:3000 \
  grafana/grafana:latest
```

---

## Část 5: Setup Grafany

Otevři **`http://10.0.0.X:3000`**

### 5a) První login

- **Username**: `admin`
- **Password**: `admin`
- → požádá tě o nové heslo, zadej silné a zapamatuj

### 5b) Přidat InfluxDB jako data source

1. Vlevo dole **⚙ Connections** → **Data sources** → **Add data source**
2. Najdi **InfluxDB**
3. Nastav:
   - **Name**: `InfluxDB` (přesně tak)
   - **Query Language**: **Flux**
   - **URL**: `http://localhost:8086`
   - **Access**: `Server (default)`
   - **Auth**: vše vypnuto
   - **InfluxDB Details**:
     - **Organization**: `home`
     - **Token**: ten samý token z kroku 3b (nebo si vytvoř další "Read Only" v InfluxDB UI)
     - **Default Bucket**: `solarguard`
4. **Save & test** → měl bys vidět zelené **"datasource is working. 1 buckets found"**

### 5c) Import dashboardů

Pro každý ze 3 JSON souborů:

1. Vlevo **Dashboards** → **New** → **Import**
2. **Upload JSON file** → vyber `solar-overview.json` z `/home/pi/solarguard/grafana/dashboards/`
3. V **DS_INFLUXDB** vyber **InfluxDB** (právě vytvořený data source)
4. **Import**

Stejně pro `spa-heating.json` a `energy-spot.json`.

### 5d) Pošli si zástupce na home screen

V Grafaně **Dashboards** → **Solar Overview** → **Star** ⭐ aby byl prvotní.

---

## Část 6: Vzdálený přístup (volitelné)

### 6a) Tailscale (doporučeno)

Pokud máš Tailscale (`tailf2eb59.ts.net`):

```bash
# Grafana je defaultně na portu 3000, dostupný v Tailnet:
# http://solarguard.tailf2eb59.ts.net:3000
```

Nebo Tailscale Funnel pro HTTPS přes internet:

```bash
sudo tailscale serve --bg --https=443 http://localhost:3000
sudo tailscale funnel --bg 443
```

Pak `https://solarguard.tailf2eb59.ts.net` je veřejně dostupné (jen přes Tailscale URL).

### 6b) Reverse proxy (advanced)

Nginx nebo Caddy - není v scope tohoto návodu.

---

## Co dál

Po instalaci:

1. **Počkej cca 1 hodinu** ať máš dost dat na pěkné grafy
2. Otevři **Solar Overview** dashboard - uvidíš:
   - Aktuální SOC, FV výkon, přebytek, spotřeba
   - Time series za posledních 24h
   - Distribuci po fázích L1/L2/L3
3. **Vířivka** dashboard - po prvním ohřevu vody:
   - Aktuální teplota vs cíl
   - Heating curve samples (každý ohřev = 1 bod)
   - Learned correction (jak moc se realita liší od fyziky)
4. **Energy & Spot** - po několika dnech:
   - Hodinový graf spot cen za týden
   - Denní energie sloupcový graf
   - Korelace teplota venku vs spotřeba

## Troubleshooting

### "No data" v Grafaně

- Zkontroluj že **Více → Long-term storage** ukazuje `PŘIPOJENO`
- V Grafaně data source: **Save & test** musí být zelené
- Zkus query přímo v InfluxDB UI: **Data Explorer** → vyber bucket `solarguard` → vyber measurement `solar` → běž zpět 1h

### InfluxDB write 401 Unauthorized

- Token není správný nebo nemá write permissions
- Vytvoř nový token v InfluxDB UI s "All Access" + zkopíruj ho do configu

### Disk se plní

- InfluxDB ukládá ~100 MB/měsíc při 30s frekvenci
- Pokud RPi má jen 16 GB SD kartu, nastav retention na 90 days místo 1 year
- Nebo přesun InfluxDB na NAS

### Grafana panely říkají "No data"

- Klikni na panel → **Edit** → **Run queries**
- Zkontroluj **Time range** vpravo nahoře (default je posledních 24h)
- Pokud máš data jen 1h, přepni na "Last 1 hour"

### Heating curve dashboard prázdný

- Heating samples se zapisují **až po dokončeném ohřevu** (delta ≥ 1°C, čas 15-900 min)
- Pokud jsi vířivku po nasazení v3.8 ještě nemál topit, žádné samples nejsou
- První ohřev ze 25→33°C → uvidíš první bod na grafu

## Co dál v roadmapě

| Verze | Téma |
|---|---|
| **v3.9** | API auth tokens, systemd watchdog, /healthz endpoint |
| **v4.0** | Anomaly detection ("dneska je výroba o 40% nižší než průměr") |
| **v4.1** | Weekly digest emaily, smart insights |
