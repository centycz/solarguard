# SolarGuard v4.1.5 - Test suite + 2 bugfixy nalezené testy

Spustil jsem **kompletní test suite** logiky vířivky proti aktuálnímu kódu (24 scénářů). Během testů jsem **našel 2 reálné bugy**, opravil je, a teď máme **24/24 PASS**.

## 🎉 Test Suite Results

```
═══════════════════════════════════════════════════════════════
SolarGuard v4.1.5 - Test suite logiky vířivky
═══════════════════════════════════════════════════════════════

═══ ZÁKLADNÍ SCÉNÁŘE ═══
✓ Stabilni prebytek 3500W, SOC 90% -> ZAPNOUT
✓ Maly prebytek 1000W (<1500 prah) -> IDLE
✓ Voda 38C = cil 38C -> nezatapej
✓ Voda 40.5C > max 40C -> hard stop

═══ BEZPEČNOST ═══
✓ Mraz -2C -> SAFE_MODE
✓ Victron MQTT stale 200s -> SAFE_MODE
✓ Spa offline (5+ failures) -> SAFE_MODE
✓ Spa error E94 -> SAFE_MODE
✓ SOC 15% < hard min 20% -> IDLE

═══ HYSTERÉZE (min_on/min_off) ═══
✓ IDLE 100s (<300s min_off) -> nesmi zapnout
✓ HEATING 200s, prebytek klesl -> drz topeni (min_on)
✓ HEATING 1000s, prebytek 200W stabilne pod 800 -> COOLDOWN
✓ COOLDOWN 100s (<300s) -> drz cooldown
✓ COOLDOWN 400s (>300s) -> IDLE

═══ SPIKE PROTECTION ═══
✓ Skok +2300W na L1 (vrivka na L2) -> ignoruj, top dal
✓ Skok +2300W na L2 (faze virivky), maly prebytek -> SPIKE_COOLDOWN
✓ L1 = 4500W > phase_max 3500W -> SPIKE_COOLDOWN (Multiplus shutdown)
✓ SPIKE_COOLDOWN aktivni - cekame
✓ SPIKE_COOLDOWN skoncil -> IDLE explicitne (v4.1.2 fix)

═══ STRATEGIE DNE ═══
✓ SURVIVE strategy bez ohledu na prebytek -> IDLE
✓ AGGRESSIVE strategy + 500W prebytek -> ZAPNOUT

═══ BAT-FULL CHOVÁNÍ ═══
✓ SOC 98%, prebytek 100W -> topi dal (BAT-FULL vybiji)

═══ ANTI-GLITCH ═══
✓ Glitch: surplus 200W ted ale max za 60s = 2500W -> ignoruj

═══ OVERRIDE ═══
✓ Override aktivni - zustane v HEATING bez ohledu na vse

═══════════════════════════════════════════════════════════════
Celkem: 24 | PASS: 24 | FAIL: 0
✓ Všechny testy prošly!
═══════════════════════════════════════════════════════════════
```

## 🐛 Bug #1: `current_scene` AttributeError při startu service (KRITICKÝ)

### Co se dělo
`current_scene` se používal v `decision.py`:
```python
if ctx.current_scene != "heat_now" and not ctx.override_active:
```

Ale **nebyl definovaný** v `SystemContext` dataclass! Hodnota se vytvořila jen dynamicky:
- V `web.py` přes `ctx.current_scene = "heat_now"` (klik na scénu)
- V `preshower.py` přes `ctx.current_scene = "solar_auto"`

### Důsledek
**Při čerstvém startu service** (před prvním klikem scény) → `AttributeError: 'SystemContext' object has no attribute 'current_scene'`

To by způsobilo crash celého rozhodovacího cyklu. Pravděpodobně se to projevovalo jako "service nereaguje" - vlákno zemřelo s exception.

### Fix
V `state.py`:
```python
# v4.1.5 FIX: current_scene nyní jako field s defaultem
current_scene: str = "solar_auto"
```

Teď ten atribut **vždy existuje** s rozumným defaultem.

## 🐛 Bug #2: Phase overload měl generický reason

### Co se dělo
Když Multiplus II hrozí shutdown (jakákoli fáze >3500W), `_load_spike_detected()` vrátil True, ale v `decide()` se napsal **generický** reason:
```
"cizi skok odberu v domacnosti"
```

### Důsledek
V Událostech a logu jsi neviděl **proč přesně** to vypnulo. "Cizí skok" může být na L1 (kuchyň), L2 (vířivka samotná), L3 (cokoliv) - **nebo phase overload (kritický!)**.

### Fix
Decision engine teď uloží konkrétní důvod:
- `"phase overload: L1=4500W > 3500W (Multiplus protection)"`
- `"L2 skok odberu +2300W na fazi virivky"`

Vidíš v Událostech přesně co se stalo a můžeš správně reagovat.

## 🧪 Test Suite (NEW)

V repo je nyní `tests/test_logic.py` který automatizovaně testuje **24 scénářů**.

### Spuštění na RPi:
```bash
cd /home/pi/solarguard
.venv/bin/python tests/test_logic.py
```

Výstup:
1. **Barevný report** v terminálu (PASS/FAIL pro každý scénář)
2. **HTML report** v `tests/test_logic_report.html`
3. **Exit code 0** pokud vše OK, **1** pokud cokoli selhalo

### Co testuje:
| Kategorie | Co testuje |
|---|---|
| BASIC | Zapnutí/vypnutí podle přebytku, dosažení teploty, max temp hard stop |
| FROST | Mráz pod 2°C → SAFE_MODE |
| STALE | Victron MQTT > 120s stará → SAFE_MODE |
| OFFLINE | Spa nedostupná 5+ pokusů → SAFE_MODE |
| ERROR | Spa error E94 → SAFE_MODE |
| SOC | Hard min SOC porušen → IDLE |
| HYST | Min on/off times - hystereze proti chvostěnému spínání |
| SPIKE-L1 | Skok na L1 (kuchyň) - ignoruj, vířivka na L2 |
| SPIKE-L2 | Skok na L2 (fáze vířivky) - vypni |
| PHASE-OVL | Jakákoli fáze >3500W - vypni (Multiplus protection) |
| SPIKE-DONE | Po cooldownu → IDLE explicitně (v4.1.2 fix) |
| STRATEGY | SURVIVE blokuje, AGGRESSIVE povoluje při menším přebytku |
| BAT-FULL | SOC 98% topí i s minimálním přebytkem |
| GLITCH | Anti-glitch - jednorázový propad PV se ignoruje |
| OVERRIDE | Manual override blokuje rozhodování |

### Co nelze testovat takto:
- **NIGHT_OFF** - závisí na real-time hodinách. Doporučuji testovat ručně:
  - V noci (po setmění + 60min) by service měla logovat `NIGHT OFF: po zapadu slunce`
  - Ráno (před východem - 30min) by měla logovat `morning resume`

## 🚀 Deploy v4.1.5

```bash
sudo systemctl stop solarguard
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
tar czf solarguard-pred-v415-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/

# WinSCP - rozbal solarguard-v4.1.5-FULL.zip do /home/pi/solarguard/

sudo chown -R pi:pi /home/pi/solarguard/
sudo systemctl start solarguard

# Spust testy aby ses ujistil ze vsechno bezi
cd /home/pi/solarguard
.venv/bin/python tests/test_logic.py

# Mel bys videt: "Celkem: 24 | PASS: 24 | FAIL: 0"
```

## 📊 Po deploy: Skutečné monitoring scénáře

V dalších dnech sleduj v PWA Události tyto scénáře v reálu:

### ✅ Test 1: Skok L1 (kuchyně)
1. Vířivka topí
2. Zapni varnou desku, troubu, vodní vařič (skok na L1)
3. **Mělo by**: vířivka topí dál, žádná SPIKE_COOLDOWN

### ✅ Test 2: Skok L2 (fáze vířivky)
1. Vířivka topí na L2
2. Zapni další velký spotřebič na L2 (sušička? bojler?)
3. **Mělo by**: SPIKE_COOLDOWN s reason `"L2 skok odberu"`
4. Po 10min: cooldown skončil → IDLE

### ✅ Test 3: BAT-FULL
1. Slunný den, baterie nabitá na 98%
2. **Mělo by**: vířivka topí i když přebytek je minimální (vybíjíme baterku)

### ✅ Test 4: STATE DRIFT (z v4.1.2)
1. Vířivka topí (state=heating, heater=ON)
2. Manuálně klikni Heater OFF v PWA Vířivka tab
3. Do 30s by se v logu mělo objevit:
   ```
   STATE DRIFT: state=HEATING ale heater_on=False -> opravim
   -> setting heater to True
   ```

Pošli mi screenshot Událostí po pár dnech provozu! 📸

## Co dál

Až bude v4.1.5 **24/24 PASS i v provozu** (žádné nečekané spike, state drift):

| Priorita | Co |
|---|---|
| 1️⃣ | InfluxDB + Grafana dashboardy (mám 3 v ZIP, jen je naimportovat) |
| 2️⃣ | Tab "Rozhodnutí" doplnit obsah |
| 3️⃣ | Tab "Plán" doplnit obsah |
| 4️⃣ | v4.2: Spotřebiče integrace |
| 5️⃣ | v5.0: Hardware (DS18B20) |

---

## TL;DR

**24/24 testů prošlo. Našel jsem 2 reálné bugy a opravil je. v4.1.5 je production-ready.**
