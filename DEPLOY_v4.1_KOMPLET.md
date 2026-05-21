# 🚀 SolarGuard v4.1 - Kompletní ranní deploy

**Cíl**: za jeden krok dostat se z aktuální verze (v3.7.5) na **v4.1** + nasadit InfluxDB + Grafana + zapnout auth s rodinným multi-user.

**Odhadovaný čas**: 30-45 minut včetně Grafany.

---

## Co dostaneš po dokončení

✅ SolarGuard v4.1 s opravami posledních 6 verzí  
✅ Insights v Přehledu - upozornění "FV výroba pod průměrem" atd.  
✅ Týdenní digest každou neděli 18:00 + manuální generování  
✅ InfluxDB 2.x + Grafana s 3 hotovými dashboardy  
✅ API auth s 3 rolemi (owner/family/guest)  
✅ Systemd watchdog (auto-restart při zamrznutí)  
✅ Manželka a děti můžou ovládat vířivku přes web s vlastními tokeny  
✅ Hosti dostanou guest token = jen čtení  

---

## ČÁST 1: Backup + zastavení (2 min)

```bash
ssh pi@solarguard

# Zastav aktuální verzi
sudo systemctl stop solarguard

# Záloha všeho
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
tar czf solarguard-backup-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/

ls -la solarguard-backup-*.tar.gz
```

**Pokud něco selže, vrátíš se přes:**
```bash
sudo systemctl stop solarguard
cd /home/pi && rm -rf solarguard
tar xzf solarguard-backup-${DATE}.tar.gz
sudo systemctl start solarguard
```

---

## ČÁST 2: Update SolarGuard (5 min)

### 2a) Vytvoř adresář pro insights (NOVÝ v v4.0!)

```bash
mkdir -p /home/pi/solarguard/solarguard/insights
```

### 2b) Stáhni a rozbal `solarguard-v4.1-FULL.zip` na PC

V ZIPu je vše. Nahraj přes WinSCP do `/tmp` na RPi:

```bash
# Na RPi:
cd /tmp
ls *.py *.yaml *.service *.json 2>/dev/null
```

### 2c) Nakopíruj soubory na správná místa

```bash
cd /tmp

# Hlavní Python kód
cp main.py                          /home/pi/solarguard/main.py
cp config.yaml                       /home/pi/solarguard/config.yaml.NEW
cp solarguard.service               /home/pi/solarguard/solarguard.service.NEW

# solarguard/ moduly
cp solarguard/auth.py                /home/pi/solarguard/solarguard/auth.py
cp solarguard/web.py                 /home/pi/solarguard/solarguard/web.py
cp solarguard/state.py               /home/pi/solarguard/solarguard/state.py
cp solarguard/engine/decision.py     /home/pi/solarguard/solarguard/engine/decision.py
cp solarguard/engine/planner.py      /home/pi/solarguard/solarguard/engine/planner.py
cp solarguard/sources/openmeteo.py   /home/pi/solarguard/solarguard/sources/openmeteo.py
cp solarguard/storage/influx.py      /home/pi/solarguard/solarguard/storage/influx.py

# NOVÝ insights adresář
cp solarguard/insights/__init__.py   /home/pi/solarguard/solarguard/insights/__init__.py
cp solarguard/insights/anomaly.py    /home/pi/solarguard/solarguard/insights/anomaly.py
cp solarguard/insights/digest.py     /home/pi/solarguard/solarguard/insights/digest.py

# Owner (pi)
sudo chown -R pi:pi /home/pi/solarguard/
```

### 2d) Merge config.yaml ručně

Tvůj starý `config.yaml` má hodnoty co fungují (Cerbo IP, OTE token, Loxone). Nový `config.yaml.NEW` má přidané sekce. Pojďme to spojit:

```bash
# Otevři starý config a postupně doplň nové sekce
sudo nano /home/pi/solarguard/config.yaml
```

Přidej na konec souboru tyto **nové sekce** (kopíruj z `config.yaml.NEW`):

```yaml
# === v3.9 NEW: API auth ===
api:
  auth_enabled: false              # ZAČNI S FALSE! Otestuj v4.1 nejdřív
  token: ""                        # nech prázdné, na to se vrátíme v ČÁSTI 5
  auth_read_too: false
  allow_localhost: true

# === v3.8 NEW: InfluxDB ===
influxdb:
  enabled: false                   # Začni s FALSE! Zapneme v ČÁSTI 4
  url: "http://localhost:8086"
  org: "home"
  bucket: "solarguard"
  token: "ZMENNAME"
  location_tag: "bojanovice"

# === v4.0 NEW: Anomaly detection ===
insights:
  window_days: 14
  min_baseline_days: 3

# === v4.1 NEW: Weekly digest ===
digest:
  slack_webhook: null
  telegram_bot_token: null
  telegram_chat_id: null
```

Ulož (`Ctrl+O`, `Enter`, `Ctrl+X`).

### 2e) Update systemd service (watchdog!)

```bash
sudo cp /home/pi/solarguard/solarguard.service.NEW /etc/systemd/system/solarguard.service
sudo systemctl daemon-reload
```

### 2f) Spusť SolarGuard a sleduj

```bash
sudo systemctl start solarguard
sudo journalctl -u solarguard -f
```

**Měl bys vidět během 1 minuty:**
```
SolarGuard v4.1 starting (dry_run=False)
[INFO] auth: API auth disabled
[INFO] insights: Loaded 0 daily summaries from /var/log/solarguard/daily_summaries.json
[INFO] digest: Digest generator starting (sunday 18:00 trigger)
[INFO] main: systemd watchdog active (notify every 45s)
Forecast: predicted PV today=...
Plan: normal | PV_rem=...
```

Pokud vše OK, otevři PWA na iPhone - mělo by to fungovat normálně. **Smaž starou PWA z home screen a přidej znovu** (nový Service Worker s v4.1).

✋ **STOP - check-point**: ujisti se že vše funguje v auth-disabled módu **PŘED** pokračováním. Otevři PWA, klikni Heater ON/OFF, ověř že Anomaly detector loguje běžně.

---

## ČÁST 3: InfluxDB instalace (10 min)

### 3a) Přidat InfluxData repo a instalovat

```bash
# Repo
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c
cat influxdata-archive_compat.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | sudo tee /etc/apt/sources.list.d/influxdata.list

sudo apt-get update
sudo apt-get install -y influxdb2

sudo systemctl enable influxdb
sudo systemctl start influxdb

# Test
curl -s http://localhost:8086/health | head
# Expect: {"name":"influxdb","status":"pass",...}
```

### 3b) Initial setup wizard

V prohlížeči otevři **`http://10.0.0.X:8086`** (kde X je IP tvého RPi - zjisti přes `hostname -I`).

1. **Get Started** → **Continue**
2. **Username**: `pi`
3. **Password**: silné heslo (8+ znaků) - **zapamatuj si!**
4. **Organization Name**: `home` (přesně tak)
5. **Bucket Name**: `solarguard` (přesně tak)
6. **Continue**

### 3c) Vytvoř API token

1. Vlevo **Load Data** → **API Tokens**
2. **Generate API Token** → **All Access API Token**
3. **Description**: `SolarGuard writer`
4. **Save**
5. **ZKOPÍRUJ TOKEN** (začíná něčím jako `pq3O2k...`) - po zavření okna ho už neuvidíš!

Ulož si ho do hesla nebo poznámky.

### 3d) Retention 1 rok

Vlevo **Load Data** → **Buckets** → klik na `solarguard`:
- **Settings** → **Edit Retention** → **1 year** → **Save**

### 3e) Aktualizuj SolarGuard config s tokenem

```bash
sudo nano /home/pi/solarguard/config.yaml
```

Najdi sekci `influxdb:` a uprav:

```yaml
influxdb:
  enabled: true                    # ZAPNI!
  url: "http://localhost:8086"
  org: "home"
  bucket: "solarguard"
  token: "pq3O2k_TVUJ_TOKEN_TADY_paste_z_kroku_3c"
  location_tag: "bojanovice"
```

Restart:
```bash
sudo systemctl restart solarguard
sudo journalctl -u solarguard -f | grep -i influx
```

**Měl bys vidět:**
```
[INFO] influx: InfluxDB connected: http://localhost:8086 bucket=solarguard
```

V PWA → Více → 🗄 Long-term storage:
> ● PŘIPOJENO  
> Zapsáno bodů: 18 (počet roste o ~5 každých 30s)

---

## ČÁST 4: Grafana instalace (10 min)

### 4a) Instalace

```bash
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee -a /etc/apt/sources.list.d/grafana.list

sudo apt-get update
sudo apt-get install -y grafana

sudo systemctl enable grafana-server
sudo systemctl start grafana-server
```

### 4b) Login

V prohlížeči **`http://10.0.0.X:3000`**

- Username: `admin`
- Password: `admin`
- → požádá tě o nové heslo (zadej silné)

### 4c) Přidat InfluxDB datasource

1. Vlevo **⚙ Connections** → **Data sources** → **Add data source**
2. Najdi **InfluxDB** → klik
3. Nastav:
   - **Name**: `InfluxDB` (přesně - dashboardy očekávají toto jméno)
   - **Query Language**: **Flux**
   - **URL**: `http://localhost:8086`
   - **Auth**: vše vypnuto
   - **InfluxDB Details**:
     - **Organization**: `home`
     - **Token**: ten samý token z 3c (nebo si vytvoř další "Read Only" v InfluxDB UI - ten má jen `read:` permissions)
     - **Default Bucket**: `solarguard`
4. **Save & test** → musí být zelené "datasource is working. 1 buckets found"

### 4d) Import 3 dashboardů

JSON soubory dashboardů jsou v `solarguard-v4.1-FULL.zip` ve složce `grafana/dashboards/`. Nahraj je přes WinSCP na RPi nebo si stáhni přímo na PC.

V Grafaně postupně pro každý:

1. Vlevo **Dashboards** → **New** → **Import**
2. **Upload JSON file**:
   - `solar-overview.json`
   - `spa-heating.json`
   - `energy-spot.json`
3. V **DS_INFLUXDB** vyber **InfluxDB** (právě vytvořený)
4. **Import**

### 4e) Hvězdičky pro rychlý přístup

V Grafaně **Dashboards** → klikni na hvězdičku ⭐ u **Solar Overview** aby byl jako default.

✋ **STOP - check-point**: počkej 10 minut ať máš data, pak otevři Solar Overview - měl bys vidět:
- SOC gauge se aktuální hodnotou
- FV výkon stat
- Time series s posledními body

Pokud "No data", zkontroluj:
```bash
# V InfluxDB UI: Data Explorer → Bucket: solarguard → vyber measurement "solar"
# Jestli tam jsou body, problém je v Grafana datasource
# Jestli nejsou, problém je že SolarGuard neposílá - check log
sudo journalctl -u solarguard | grep -i influx | tail -20
```

---

## ČÁST 5: Multi-user auth (10 min)

Teď zapneme auth s rodinným multi-userem. **DŮLEŽITÉ**: nech si někde po ruce token!

### 5a) Zapni auth v configu

```bash
sudo nano /home/pi/solarguard/config.yaml
```

Najdi sekci `api:` a uprav:

```yaml
api:
  auth_enabled: true               # ZAPNI!
  token: ""                        # Nech prázdné - vygeneruje se náhodný owner token
  auth_read_too: false             # GET endpointy zůstanou veřejné (pro Grafanu)
  allow_localhost: true            # Skripty na RPi fungují bez tokenu
```

### 5b) Restart a získání bootstrap tokenu

```bash
sudo systemctl restart solarguard
sudo journalctl -u solarguard -n 50 | grep -A 5 "random owner token"
```

**Uvidíš velký banner:**
```
======================================================================
Auth enabled but no users! Generated random owner token:
  T7K2-mB9pXfGhJk_LqRsTuVwXyZaB2cD3eF4gH5iJ6k
Use this token to login as 'owner', then create more users via UI.
Token IS persisted - won't change on next restart.
======================================================================
```

**Zkopíruj ten token a ulož si ho!** Bez něj se nepřihlásíš.

### 5c) Login do PWA

1. Otevři PWA na iPhone (smaž z home screen a přidej znovu pokud je cache problém)
2. Zobrazí se **modal "Vlož API token"**
3. Vlož token z 5b
4. **Přihlásit**
5. ✓ Měl bys vidět úplný dashboard, **vpravo nahoře pill `👤 owner`**

### 5d) Vytvoř další uživatele (rodina + hosti)

V PWA → **Více** (⚙) → najdeš novou položku **"👥 Uživatelé"** (jen pro owner role)

Pro každého člena rodiny:
1. Klikni **"+ Přidat"**
2. **Jméno**: např. `manzelka`, `syn`, `dcera`
3. **Role**: **family** (ovládají vířivku, vidí data)
4. **Token**: klikni **"Generovat"** → automaticky vyplní 32-char random
5. **Přidat**
6. ✓ Zobrazí se token v alertu - **pošli ho rodinnému členu** (přes signal, sms, mail)
7. Token najdeš jen v hash podobě v `users.json`, plain-text vidíš jen tady jednou

Pro hosty:
1. **+ Přidat** → **Role: guest** → token gen → **Přidat**
2. Pošli token hostovi - může se přihlásit, ale tlačítka budou zašedlá

### 5e) Bezpečnostně-síťové aspekty

| Scénář | Co dělat |
|---|---|
| **Lokálně z domácí LAN** | Vše funguje, auth platí |
| **Tailscale** | Funguje stejně, auth platí |
| **Tailscale Funnel (HTTPS přes net)** | DOPORUČENO mít auth zapnutý! Bez něj může cizí ovládat vířivku |
| **`curl localhost:8000/...` na RPi** | Funguje bez tokenu (`allow_localhost: true`) |

✋ **STOP - check-point**: vyzkoušej:
1. Owner token funguje, vidíš všechno
2. Logout (klikni 👤 owner pill nahoře) → zobrazí se znovu modal
3. Vlož family token → tab Uživatelé není vidět (správně, jen owner)
4. Family může klikat Heater ON/OFF
5. Vlož guest token → tlačítka jsou zašedlá

---

## ČÁST 6: Volitelně Telegram digest (5 min)

Jen pokud chceš každou neděli 18:00 dostat zprávu do Telegramu.

### 6a) Vytvoř Telegram bota

1. Otevři **https://t.me/BotFather** v Telegramu
2. Zadej `/newbot`
3. Bot name: `SolarGuard Bojanovice`
4. Bot username: něco jako `bojanovice_solar_bot`
5. **Ulož TOKEN** který ti BotFather dá

### 6b) Najdi své chat_id

1. **Pošli zprávu novému botovi** (jakkoukoli, třeba `/start`) - jinak ti bot nemůže psát
2. Otevři **https://t.me/userinfobot** → `/start` → uvidíš svoje **ID** (číslo)

### 6c) Přidej do configu

```bash
sudo nano /home/pi/solarguard/config.yaml
```

```yaml
digest:
  slack_webhook: null
  telegram_bot_token: "1234567890:ABCdef_TVUJ_TOKEN_OD_BOTFATHER"
  telegram_chat_id: "123456789"  # tvoje ID
```

```bash
sudo systemctl restart solarguard
```

### 6d) Test

V PWA → **Týdenní** → klikni **"⚡ Generovat teď"** → mělo by ti přijít na Telegram krásně formátovaný digest.

Od teď každou neděli 18:00 ti automaticky přijde zpráva.

---

## ČÁST 7: Vzdálený přístup přes Tailscale (volitelné)

Pokud chceš PWA z venku přes mobilní data:

### 7a) Tailscale (pokud ještě nemáš)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Otevři URL co ti dá, přihlaš se přes Google/email
```

### 7b) Funnel pro veřejnou HTTPS URL

```bash
sudo tailscale serve --bg --https=443 http://localhost:8000
sudo tailscale funnel --bg 443
```

Teď máš veřejně dostupné `https://solarguard.tailf2eb59.ts.net` - **proto je auth tak důležitý!** Bez něj by si kdokoli mohl spustit Heater.

V iPhone PWA změň URL z `http://10.0.0.X:8000` na `https://solarguard.tailf2eb59.ts.net` a smaž starou PWA + přidej znovu.

---

## ČÁST 8: Sanity check (2 min)

Po všem ověř:

```bash
# Status systemd
sudo systemctl status solarguard
# Expect: active (running), Watchdog set
sudo systemctl status influxdb
sudo systemctl status grafana-server

# Health endpoint
curl http://localhost:8000/healthz | python3 -m json.tool
# Expect: {"status":"ok","version":"v4.1",...}

# Posledních 50 řádků logu
sudo journalctl -u solarguard -n 50 --no-pager

# Disk usage (po týdnu se podívej znovu)
df -h /
du -sh /var/lib/influxdb2/  # bude růst, cca 50-100MB / měsíc
```

---

## 🎉 Hotovo!

Pokud všechno proběhlo OK, máš:

| Co | Kde |
|---|---|
| 🌞 SolarGuard PWA | `http://10.0.0.X:8000` |
| 📊 InfluxDB UI | `http://10.0.0.X:8086` |
| 📈 Grafana | `http://10.0.0.X:3000` |
| ❤ Health check | `http://10.0.0.X:8000/healthz` |
| 🔐 Owner token | (uložený v poznámkách/heslech) |
| 👨‍👩‍👧‍👦 Family tokens | (předané rodině) |

## 🐛 Když něco selže

### "Spa is offline" 503
- TCP connection k vířivce padla. Restart pomáhá: `sudo systemctl restart solarguard`
- v4.1 má quick reconnect, neměl by být tak častý

### Anomaly insights nic neukazují
- Potřebují aspoň 3 dny dat → po nasazení nebudou hned aktivní
- A některé alerty se aktivují až po 18h / 20h (podle typu)

### Digest Tab "Žádný digest zatím"
- Spravně - generuje se v neděli 18:00, nebo manuálně tlačítkem
- Klikni **⚡ Generovat teď**

### Grafana "No data"
- V InfluxDB UI: **Data Explorer** → vyber bucket `solarguard` → vyber `solar` → vidíš data?
- Pokud ano: problém je v Grafana datasource (špatný token nebo URL)
- Pokud ne: SolarGuard neposílá → `journalctl -u solarguard | grep -i influx`

### Token zapomenut
```bash
# SSH na RPi
sudo nano /home/pi/solarguard/config.yaml
# Změň auth_enabled: false
sudo rm /var/log/solarguard/users.json    # smaže všechny uživatele!
sudo systemctl restart solarguard
# Při dalším startu se vygeneruje nový owner token, viz log
```

### Watchdog restartuje SolarGuard
```bash
sudo journalctl -u solarguard | grep -i watchdog
# Pokud "main loop stuck" → bug, pošli mi log
# Pokud "service exited" → zkontroluj memorymax / cpu
```

---

## Co dál

Teď pár dnů sleduj:
- **Insights** v Přehledu - kdy se objeví, jaké
- **Tab Týdenní** - po týdnu uvidíš první automatický digest
- **Grafana** - hraj si s time range, různými dashboardy

Pak se ozvi co bys chtěl dál:
- 🍳 **v4.2 Spotřebiče** - "kdy můžu pustit pračku" alerty
- 🌡 **v5.0 Hardware** - DS18B20 ve vodě, přesná teplota
- 🛁 **Specifické tweaks** podle reálného provozu
