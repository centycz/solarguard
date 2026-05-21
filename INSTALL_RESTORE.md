# SolarGuard v3.7.1 - obnovení od nuly

Smazal jsi celý `/home/pi/solarguard` a nemáš zálohu? **Žádný problém.** Tenhle ZIP obsahuje **kompletní aplikaci** + setup script. Po dokončení budeš mít přesně to, co jsi měl - jen bez heating_history.jsonl (to se naplní samo až bude vířivka topit).

## 1) Nahrání ZIPu na RPi

**Přes WinSCP / FileZilla / scp:**

Stáhni `solarguard-v3.7.1-FULL.zip` a přenes ho na RPi do `/home/pi/`.

Nebo přes scp z PC:
```bash
scp solarguard-v3.7.1-FULL.zip pi@10.0.0.X:/home/pi/
```

## 2) Rozbalit a setup

Připoj se SSH na RPi a:

```bash
cd /home/pi

# Pro jistotu - smaz pripadne zbytky
rm -rf solarguard

# Rozbalit
unzip solarguard-v3.7.1-FULL.zip

# Pokud se rozbalí jako solarguard-v3.7.1-FULL/, prejmenuj na solarguard
ls -la
# pokud vidis solarguard-v3.7.1-FULL adresar misto solarguard:
mv solarguard-v3.7.1-FULL solarguard

cd solarguard
ls -la
# Mel bys videt: main.py, config.yaml, setup.sh, requirements.txt, solarguard/, ...
```

## 3) Spustit setup

```bash
chmod +x setup.sh
./setup.sh
```

Setup automaticky:
1. Nainstaluje Python venv (apt-get)
2. Vytvoří `.venv` a nainstaluje všechny Python balíčky
3. Vytvoří `/var/log/solarguard` adresář
4. Nainstaluje systemd unit + povolí autostart

Trvá to cca 3-5 minut na RPi 4. Při instalaci pip balíčků uvidíš pár warningů (normální).

## 4) Zkontrolovat config

```bash
nano /home/pi/solarguard/config.yaml
```

Hlavně zkontroluj že IP adresy odpovídají tvé síti:

```yaml
victron:
  host: "10.0.0.200"     # Cerbo GX

loxone:
  host: "10.0.0.88"      # Loxone Miniserver
  username: "solarguard"
  password: "123456FVE"

spa:
  host: "10.0.0.115"     # Intex 28462
```

A případně **OTE-CR** - jestli máš nakonfigurované poplatky:

```yaml
spot_pricing:
  enabled: true
  fee_kc_per_kwh: 1.5     # ← uprav podle TVÉHO tarifu (1-3 Kč obvykle)
```

## 5) Spustit

```bash
sudo systemctl start solarguard
sudo journalctl -u solarguard -f
```

V logu by měly proletět:

```
SolarGuard v3.7.1 starting (dry_run=False)
>>> OSTRY PROVOZ - virivka bude REALNE ovladana <<<
Heating curve init: volume=1098.0L, heater=2200.0W, physical baseline = 58.0 min/°C
Heating curve: loaded 0 historical samples
MQTT connected to 10.0.0.200:1883
Spa connected at 10.0.0.115
Loxone source starting (10.0.0.88, poll 60s)
Open-Meteo source starting (lat=48.859, ...)
OTE source starting...
Scheduler starting: 3 rules, global_enabled=True
Waiting 30s for initial data (MQTT + forecast)...
[idle->idle] [unknown] SOC=85% PV=4500W surplus=2800W water=37C ...
```

Pokud vidíš tyhle řádky a `Plan: NORMAL | ...` po cca 1 minutě, jsi **plně zpátky online**.

## 6) Web UI

Otevři v prohlížeči:

```
http://10.0.0.X:8000
```

(kde X je IP RPi - zjistíš `hostname -I`)

Nebo přes Tailscale:
```
https://solarguard.tailf2eb59.ts.net
```

## 7) PWA na iPhone

1. Safari → otevři URL
2. Sdílet → "Přidat na plochu"
3. Příště otevři přímo z home screen (fullscreen mód)

## 8) Co je v této verzi (v3.7.1)

Vše co bylo - kompletní rekapitulace:

| Feature | Verze |
|---|---|
| ✅ Solar surplus → vířivka topení | v1.x |
| ✅ Daily strategy (aggressive/normal/conservative/survive) | v2.x |
| ✅ BAT-FULL detekce | v3.1 |
| ✅ Anti-glitch 60s stability | v3.1.1 |
| ✅ Mírný 33°C scéna pro děti | v3.2 |
| ✅ PWA podpora (offline cache) | v3.3 |
| ✅ Per-phase spike detection (L2 = vířivka) | v3.4 |
| ✅ Phase overload protection | v3.4 |
| ✅ Mobile-first UI s bottom nav | v3.5 |
| ✅ Časový plán scén | v3.6 |
| ✅ OTE-CR spotové ceny | v3.6 |
| ✅ Heating curve (predikce ohřevu) | v3.7 |
| ✅ Pre-shower mode (T-30, T-5 bublinky) | v3.7 |
| ✅ Fyzikální model 1098L | v3.7.1 |

## 9) Co NENÍ v této verzi

- ❌ InfluxDB + Grafana (rozpracované v v3.8 - dokončíme až bude online)
- ❌ Záloha heating_history.jsonl (smazal jsi ji se složkou - začneš od 0)

## 10) Troubleshooting

### `setup.sh: Permission denied`
```bash
chmod +x setup.sh
./setup.sh
```

### `apt-get` neaktualizuje
```bash
sudo apt-get update
# pokud "Could not get lock" - počkej 1 min nebo:
sudo killall apt apt-get
```

### `pip install` selhává
```bash
# Zkus znovu manuálně:
cd /home/pi/solarguard
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### `intex_spa` se neinstaluje
```bash
.venv/bin/pip install --upgrade aio-intex-spa intex-spa
```

### SolarGuard startuje ale nepřipojí se k Cerbu
- Zkontroluj IP v config.yaml (`victron.host`)
- Zkontroluj že Cerbo má povolené MQTT on LAN: Settings → Services → MQTT on LAN: Enabled
- Otestuj ze RPi: `ping 10.0.0.200`

### Web UI je na port 8000 ale nefunguje
- Zkontroluj firewall: `sudo ufw status`
- Pokud je aktivní: `sudo ufw allow 8000`

### Nemůžu spustit službu
```bash
sudo systemctl status solarguard
# pokud "service file not found":
sudo cp /home/pi/solarguard/solarguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable solarguard
sudo systemctl start solarguard
```

## 11) Až bude online

Pak doděláme **v3.8** s InfluxDB + Grafana - dlouhodobá metrika, krásné dashboardy, predikce přes měsíce.

Pro teď: **rozbalit, setup, run.** 5 minut a jsi zpátky.
