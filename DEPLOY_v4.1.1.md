# 🚀 SolarGuard v4.1.1 - Jednoduchý deploy

**RPi IP**: 10.0.0.120

Postup je rozdělený na **3 nezávislé fáze**. Po každé otestuješ že vše jede a teprve pak pokračuješ. Pokud něco selže, předchozí fáze pořád běží.

---

## FÁZE 1: SolarGuard v4.1.1 (10 min)

### Krok 1.1: Backup současného stavu

```bash
ssh pi@10.0.0.120
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
sudo systemctl stop solarguard
tar czf solarguard-backup-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/
ls -la solarguard-backup-*.tar.gz
```

### Krok 1.2: Smaž starý SolarGuard adresář (kromě .venv)

```bash
cd /home/pi/solarguard
# Smaz vse krome venv (ten obsahuje nainstalovane Python balíčky)
find . -mindepth 1 -maxdepth 1 ! -name '.venv' -exec rm -rf {} +
ls -la
# Mel bys videt jen .venv adresar
```

### Krok 1.3: Rozbal v4.1.1 přímo do /home/pi/solarguard

Stáhni `solarguard-v4.1.1-FULL.zip` na PC.

**Možnost A) Rozbal lokálně, pak nahraj WinSCP-em:**
- Rozbal ZIP na PC (např. do `solarguard-v4.1.1/`)
- WinSCP: Otevři levou stranou ten rozbalený obsah
- Pravou stranou jdi do `/home/pi/solarguard/`
- Označ vše vlevo (Ctrl+A) → přetáhni doprava
- Potvrzení "Overwrite all"

**Možnost B) Nahraj ZIP a rozbal na RPi:**
```bash
# WinSCP: nahraj solarguard-v4.1.1-FULL.zip do /home/pi/
# Pak na RPi:
cd /home/pi/solarguard
unzip -o ~/solarguard-v4.1.1-FULL.zip
ls -la
```

### Krok 1.4: Nastav vlastníka + práva

```bash
sudo chown -R pi:pi /home/pi/solarguard/
chmod +x /home/pi/solarguard/setup.sh 2>/dev/null
ls -la /home/pi/solarguard/main.py
ls -la /home/pi/solarguard/solarguard/auth.py
ls -la /home/pi/solarguard/solarguard/insights/
```

Měl bys vidět všechny soubory + adresář `insights/`.

### Krok 1.5: Vytvoř log adresář

```bash
sudo mkdir -p /var/log/solarguard
sudo chown pi:pi /var/log/solarguard
ls -la /var/log/solarguard
```

### Krok 1.6: Aktivuj nový systemd service (s watchdog)

```bash
sudo cp /home/pi/solarguard/solarguard.service /etc/systemd/system/solarguard.service
sudo systemctl daemon-reload
cat /etc/systemd/system/solarguard.service | head -15
```

Měl bys vidět `Type=notify` a `WatchdogSec=90s`.

### Krok 1.7: Spusť SolarGuard

```bash
sudo systemctl start solarguard
sleep 3
sudo journalctl -u solarguard -n 20 --no-pager
```

Hledej řádky:
```
SolarGuard v4.1.1 starting
[INFO] auth: API auth disabled
[INFO] insights: Loaded 0 daily summaries
[INFO] digest: Digest generator starting
[INFO] main: systemd watchdog active (notify every 45s)
```

### ✅ Test FÁZE 1

V prohlížeči: **http://10.0.0.120:8000**

- Vidíš dashboard
- Footer ukazuje **v4.1.1**
- Topení, FV, přebytek - vše naskočilo
- V "Více" je položka "🗄 Long-term storage" (zatím "nenakonfigurováno")

**Pokud NĚCO nejede - STOP** a pošli mi:
```bash
sudo journalctl -u solarguard -n 50 --no-pager
```

Pokud vše OK → pokračuj na FÁZI 2.

---

## FÁZE 2: InfluxDB (15 min)

### Krok 2.1: Instalace InfluxDB

```bash
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c
cat influxdata-archive_compat.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | sudo tee /etc/apt/sources.list.d/influxdata.list

sudo apt-get update
sudo apt-get install -y influxdb2

sudo systemctl enable influxdb
sudo systemctl start influxdb

# Test ze bezi
curl -s http://localhost:8086/health
```

Mělo by vrátit `{"name":"influxdb","status":"pass",...}`.

### Krok 2.2: Setup wizard

V prohlížeči: **http://10.0.0.120:8086**

1. **Get Started** → **Continue**
2. **Username**: `pi`
3. **Password**: vymysli silné (8+ znaků), ulož si ho
4. **Organization Name**: `home` (musí být přesně tak!)
5. **Bucket Name**: `solarguard` (musí být přesně tak!)
6. **Continue**

### Krok 2.3: Vygeneruj API token

V InfluxDB UI:
1. Vlevo dole **Load Data** (≡ ikona) → **API Tokens**
2. **Generate API Token** → **All Access API Token**
3. Description: `solarguard-writer`
4. **Save**
5. **❗ ZKOPÍRUJ token** (dlouhý string, např. `pq3O2k_xxx...`) - **už ho nikdy neuvidíš!**
6. Ulož ho do textu / heslového manažeru

### Krok 2.4: Retention 1 rok

V InfluxDB UI:
1. **Load Data** → **Buckets**
2. Klik na `solarguard`
3. **Settings** → **Edit Retention** → **1 year** → **Save**

### Krok 2.5: Aktualizuj SolarGuard config

```bash
sudo nano /home/pi/solarguard/config.yaml
```

Najdi sekci `influxdb:` (kolem řádky 175) a nastav:

```yaml
influxdb:
  enabled: true
  url: "http://localhost:8086"
  org: "home"
  bucket: "solarguard"
  token: "pq3O2k_xxx_TVUJ_TOKEN_TADY"
  location_tag: "bojanovice"
```

Ulož: `Ctrl+O` Enter, `Ctrl+X`.

```bash
sudo systemctl restart solarguard
sleep 5
sudo journalctl -u solarguard -n 30 --no-pager | grep -i influx
```

Měl bys vidět:
```
[INFO] influx: InfluxDB connected: http://localhost:8086 bucket=solarguard
```

### ✅ Test FÁZE 2

V PWA → **Více** → **🗄 Long-term storage**:

```
● PŘIPOJENO
URL: http://localhost:8086
Zapsáno bodů: 18 (počet roste)
Buffer: 0 / 1000
```

Pokud "● ODPOJENO" - zkontroluj token v configu, restartuj službu.

---

## FÁZE 3: Grafana (10 min)

### Krok 3.1: Instalace

```bash
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee -a /etc/apt/sources.list.d/grafana.list

sudo apt-get update
sudo apt-get install -y grafana

sudo systemctl enable grafana-server
sudo systemctl start grafana-server

# Test ze bezi
curl -s http://localhost:3000/api/health
```

Mělo by vrátit JSON s `"database":"ok"`.

### Krok 3.2: První login

V prohlížeči: **http://10.0.0.120:3000**

- Username: `admin`
- Password: `admin`
- Zadáš nové heslo (silné, ulož si)

### Krok 3.3: Přidat InfluxDB datasource

V Grafaně:
1. Vlevo dole ⚙ ikona → **Connections** → **Data sources**
2. **Add data source** → najdi a klikni **InfluxDB**
3. Nastav:

| Pole | Hodnota |
|---|---|
| **Name** | `InfluxDB` *(přesně tak, dashboardy očekávají toto jméno!)* |
| **Query Language** | **Flux** *(důležité! ne InfluxQL)* |
| **URL** | `http://localhost:8086` |
| **Auth** | nech vše vypnuto |
| **Organization** | `home` |
| **Token** | ten samý API token z kroku 2.3 |
| **Default Bucket** | `solarguard` |

4. Dole klikni **Save & test**
5. Mělo by být zelené **"datasource is working. 1 buckets found"**

### Krok 3.4: Import 3 dashboardů

Dashboardy najdeš v `solarguard-v4.1.1-FULL.zip` ve složce `grafana/dashboards/`. Tři soubory:
- `solar-overview.json`
- `spa-heating.json`
- `energy-spot.json`

Pro každý:
1. V Grafaně vlevo **Dashboards**
2. **New** (vpravo nahoře) → **Import**
3. **Upload JSON file** → vyber soubor
4. V dropdownu **DS_INFLUXDB** vyber **InfluxDB**
5. **Import**

### ✅ Test FÁZE 3

V Grafaně **Dashboards** → klikni **Solar Overview**:

- SOC gauge ukazuje aktuální hodnotu
- FV výkon stat
- Time series s body (čím déle běží, tím víc dat)

Pokud "No data" - počkej 5 minut a refresh. Pokud stále nic, v InfluxDB UI: **Data Explorer** → vyber bucket `solarguard` → vyber `solar` measurement - mělo by tam být plno dat.

---

## 🎉 HOTOVO!

Máš:

| URL | Co tam je |
|---|---|
| **http://10.0.0.120:8000** | SolarGuard PWA (přidej na home screen iPhone) |
| **http://10.0.0.120:8086** | InfluxDB UI (data explorer, retention) |
| **http://10.0.0.120:3000** | Grafana (3 dashboardy) |
| **http://10.0.0.120:8000/healthz** | Health check (bez auth) |

---

## VOLITELNÉ: Multi-user auth (10 min)

Tohle si nech až pojede vše ostatní pár dní. Pokud chceš teď, viz `CHANGELOG_v4.1.md` v ZIPu.

Stručně:
```bash
sudo nano /home/pi/solarguard/config.yaml
# api: auth_enabled: true (token nech prázdný)
sudo systemctl restart solarguard
sudo journalctl -u solarguard -n 50 | grep "random owner token"
# Ulož token, vlož do PWA login modalu
```

---

## 🆘 Když něco selže

### "Spa is offline" 503
Občasné, vyřeší se sama. Pokud trvá víc než 5 minut:
```bash
sudo systemctl restart solarguard
```

### Grafana "No data"
1. Otevři http://10.0.0.120:8086
2. Vlevo **Data Explorer** → vyber bucket `solarguard`
3. Vyber measurement (např. `solar`) → klikni **Submit**
4. Pokud **vidíš data** → problém je v Grafana datasource (chybný token nebo URL)
5. Pokud **nevidíš data** → SolarGuard neposílá:
```bash
sudo journalctl -u solarguard -n 50 | grep -i influx
```

### InfluxDB connection failed
```bash
sudo systemctl status influxdb
curl http://localhost:8086/health
```

### SolarGuard se restartuje sám
```bash
sudo systemctl status solarguard
sudo journalctl -u solarguard -n 100 --no-pager
```

Pošli mi log a vyřešíme.

### Vrácení k předchozí verzi (rollback)

```bash
sudo systemctl stop solarguard
cd /home/pi
rm -rf solarguard
tar xzf solarguard-backup-YYYYMMDD-HHMM.tar.gz
sudo cp solarguard/solarguard.service /etc/systemd/system/  # pokud byl starý service
sudo systemctl daemon-reload
sudo systemctl start solarguard
```

---

## Co budeš pozorovat v prvních dnech

**Den 1**:
- Vše chodí, sleduješ Grafana grafy v reálném čase
- V "Týdenní" tabu zatím "Žádný digest"
- V Přehledu zatím žádné Insights (potřebují 3+ dny dat)

**Den 3-7**:
- Začnou fungovat Insights (odpolední porovnání)
- Klikni "⚡ Generovat teď" v Týdenním tabu - uvidíš první preview

**Neděle 18:00**:
- Automatický týdenní digest v journalctl + UI

**Den 14+**:
- Plné Insights, delta vs minulý týden funguje

---

Užij si to! 🌞 Až bude hotovo, pošli screenshot Grafany 📸
