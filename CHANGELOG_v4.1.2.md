# SolarGuard v4.1.2 - Fix bugs (žádné nové features!)

Soustředíme se na **stabilitu**, ne přidávání. Tato verze opravuje 3 závažné bugy které jsi narazil.

## 🐛 Fix #1: STATE DRIFT - SolarGuard tvrdil že topí, ale topení bylo OFF

**Problém v 16:11+**: Vidíš `state=heating, "topi, baterka plna -> vybiji pro topeni"` ale `heater=False` ve sloupci. Realita a stav byly nesynchronizovány.

**Příčina**: Po spike protection se topení vypnulo (správně). Cooldown skončil, state se vrátil do HEATING (asi přes manuální klik nebo automatickou recovery), ale `set_heater=True` se neposlal protože decision engine si myslel že už topí.

**Oprava** (`main.py`): Přidán **state drift check** - pokud `state==HEATING` ale `heater_on==False`, automaticky pošli příkaz k zapnutí topení s warningem do logu.

```
[WARNING] STATE DRIFT: state=HEATING ale heater_on=False -> opravim
[INFO] -> setting heater to True
```

## 🐛 Fix #2: Po SPIKE_COOLDOWN se nevrátil do správného stavu

**Problém v 15:40:59**: `state_change: spike_cool -> idle (reason: fallback)` - vrátil se do IDLE jen "fallback", neudělal pořádný refresh stavu.

**Příčina**: V decision.py po skončení spike cooldownu kód propadl na fallback ("fallback noc/den") místo explicitního přechodu do IDLE.

**Oprava** (`decision.py`): Po skončení cooldownu **explicitně přejdi do IDLE** s logovanou zprávou:

```
[INFO] SPIKE cooldown ukoncen po 600s -> IDLE
```

Pak normální IDLE logika rozhodne jestli zapnout topení (pokud surplus > on_threshold + min_off_time uplynul).

## 🐛 Fix #3: Spot ceny ukazovaly cenu s distribučním poplatkem

**Problém**: PWA ukazovala "2.53 Kč" ale skutečná spot cena byla "1.027 Kč/kWh".

**Příčina**: Ke každé hodině se přičítal `fee_kc_per_kwh: 1.5` (distribuce). Ale jak správně píšeš - **distribuce se účtuje paušálně měsíčně, ne podle hodin**, takže to dezinformuje.

**Oprava**:
- API vrací **OBĚ verze**: `today_prices_kc` (s distribucí) i `today_prices_kc_clean` (čistý spot)
- PWA defaultně ukazuje **čistý spot** ("1.03 Kč" pro 16:00)
- Pod cenou je odkaz **"[přepnout]"** - klepnutím se přepne mezi čistou cenou a s distribucí, volba se uloží do localStorage

## 🐛 Bonus: Spike na L2 (fáze vířivky)

V configu už máš `spa_phase_label: "L2"` a `phase_max_continuous_w: 3500`. Tato logika **už funguje** v decision.py:

- ✅ Skok na L1 (vařivá deska, trouba) → ignoruje pokud L2 + L3 mají rezervu
- ✅ Skok na L3 → ignoruje
- ✅ Skok na L2 → vypne (vířivka topí na L2)
- ✅ Jakákoli fáze > 3500W → vypne (Multiplus II shutdown protection)

Tvůj 15:30 spike: load skočil z 2518W → 3231W = +713W. Z dat nevidím per-phase, ale pravděpodobně to byl skok na L2 (zapnula se ti pračka nebo něco). Pokud chceš, **mohu zvýšit `load_spike_threshold_w`** z 800 na třeba 1500W aby to nebylo tak citlivé. Nebo sleduj v PWA "Toky energie" abys viděl kde co bylo.

---

## 🧪 Test plán po nasazení

Po nasazení v4.1.2 si projdi tyto scénáře a sleduj v Událostech:

### Test 1: Spike recovery
1. Vířivka topí (state=heating, heater=ON)
2. Zapni něco velkého (sušička, varná deska) - simuluj cizí spike
3. Vidíš: `state_change: heating → spike_cool`, heater=OFF
4. Počkej 10 minut (`spike_cooldown_sec: 600`)
5. **Měl bys vidět**: `[INFO] SPIKE cooldown ukoncen → IDLE`
6. Po dalších ~5 minutách (min_off_time): `state_change: idle → heating`, heater=ON

### Test 2: State drift
1. V PWA Vířivka tab klikni Heater **OFF** (manuální vypnutí)
2. SolarGuard má state=heating ale heater=False
3. **Do 30s** by se mělo objevit:
   - `[WARNING] STATE DRIFT: state=HEATING ale heater_on=False`
   - `[INFO] -> setting heater to True`
   - Vířivka znovu topí

   ALE pokud chceš opravdu vypnout topení dlouhodobě, použij **scénu Solar Auto** nebo počkej - SolarGuard nepřemýšlí "user wants off", jen "hey state=heating ale neni topení, oprav to".

### Test 3: Spot cena
1. Otevři tab "Spot ceny"
2. **Měl bys vidět**: aktuální cena ~1.03 Kč (čistý spot bez distribuce)
3. Klikni odkaz **"[přepnout]"** pod cenou
4. Cena se změní na 2.53 Kč (s distribucí)
5. Refresh stránky - volba zůstává

### Test 4: Spike protection L2 only
Když jsi na vařivé desce nebo troubě (L1), vířivka by **neměla vypnout**. Pokud vypne, podívej se v Událostech na detail - reason by měl být:
- ✅ OK: `"L2 SPIKE detected: +500W na fazi virivky"`
- ❌ BUG: `"PHASE OVERLOAD: L1=4500W"` *(jen pokud opravdu kuchyň přetížila)*

Pokud potřebuješ vidět per-phase data, **otevři tab "Toky"** - tam jsou rozpočítaná L1/L2/L3.

---

## Update postup

```bash
sudo systemctl stop solarguard
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
tar czf solarguard-pred-v412-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/

# WinSCP - nahraj 3 soubory přímo přes:
#   main.py                    → /home/pi/solarguard/main.py
#   solarguard/state.py        → /home/pi/solarguard/solarguard/state.py
#   solarguard/web.py          → /home/pi/solarguard/solarguard/web.py
#   solarguard/engine/decision.py → /home/pi/solarguard/solarguard/engine/decision.py

sudo systemctl start solarguard
sudo journalctl -u solarguard -f | grep -iE "(STATE DRIFT|SPIKE|spike_cool|setting heater)"
```

Nebo prostě **rozbal celý ZIP** (jako minule) ↗ to je jednodušší.

---

## Co dál (po otestování)

| Priorita | Co |
|---|---|
| 1️⃣ TEST | Otestuj scénáře výše, sleduj Události a logy |
| 2️⃣ Tweaks | Pokud spike vypíná moc často, zvýšíme `load_spike_threshold_w` z 800 na 1500W |
| 3️⃣ User Mgmt | Pak dokončíme tab Uživatelé (oprava createUser endpoint - missing field) |
| 4️⃣ Future | Spotřebiče, hardware, atd. |

Vyřešíme jeden problém po druhém. 🎯
