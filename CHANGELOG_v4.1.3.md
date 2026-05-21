# SolarGuard v4.1.3 - User management dotaženo

Soustředíme se na **dokončení multi-user správy**. Žádné nové features mimo to.

## ✅ Co je nového

### 🎨 Nový UI tab "Uživatelé"

Místo jednoduchého seznamu teď máš **kartičky** pro každého uživatele:

```
┌────────────────────────────────────────────────┐
│  manzelka  [TY]  [FAMILY]                🔑 🗑 │
├────────────────────────────────────────────────┤
│ ROLE:        [family ▼] (ovládá vířivku, ...)  │
│ VYTVOŘEN:    28.4.2026                         │
│ POSLEDNÍ:    28.4.2026, 14:35                  │
└────────────────────────────────────────────────┘
```

### 🔄 Změna role (NEW)

Klik na dropdown **Role** ve kartičce → zvol nové role → confirm → změna proběhne. **Nemůžeš degradovat** sám sebe ani posledního ownera (validace na klientu i serveru).

### 📋 Token dialog (krásný)

Po vytvoření uživatele nebo regeneraci tokenu se zobrazí **krásný modal** s:
- Jménem, rolí, tokenem (velký, font monospace, klepni = vyber celý)
- Tlačítko **📋 Kopírovat** → automaticky do clipboardu
- Hláška podle situace: *"Předej tento token uživateli"* nebo *"⚠ Toto je tvůj vlastní token, po close se odhlásíš"*

### 🎯 Self-regen flow

Když regeneruješ **vlastní** token:
1. Confirm dialog s warningem
2. Token modal s informací "po close budeš odhlášen"
3. Po close: SolarGuard automaticky uloží **nový token do localStorage** a refreshne stránku
4. Jsi přihlášen s novým tokenem

### 🛡 Backend ochrany

- **Nemůžeš smazat sám sebe** (HTTP 400 + UI tlačítko disabled)
- **Nemůžeš degradovat sebe** z owner (HTTP 400 + UI alert)
- **Nemůžeš smazat posledního ownera** (HTTP 400 + UI tlačítko disabled)
- **Nemůžeš degradovat posledního ownera** (HTTP 400 + UI alert)
- Všechny endpointy vyžadují **explicitní `require_auth(write=True, owner_only=True)`**

### 🐛 Fix: HTTP 422 "Field required"

V4.1.1 dialog Přidat uživatele padal s `{"detail":[{"type":"missing","loc":["query","req"]}]}`. Vyřešeno - **všechny endpointy** mají teď signaturu `(req: Body, request: Request)` se správným pořadím parametrů.

### 📊 Audit endpoint

`GET /api/users/{name}/audit` - vrací posledních 50 akcí daného uživatele (kdo co kliknul). Pro budoucí UI rozšíření.

---

## 🚀 Jak to teď použít (krok za krokem)

### Krok 1: Zapni auth v configu

```bash
sudo nano /home/pi/solarguard/config.yaml
```

Najdi sekci `api:` a uprav:

```yaml
api:
  auth_enabled: true                # PŘEPNI NA TRUE
  token: ""                         # Nech prázdné, vygeneruje se náhodný owner
  auth_read_too: false              # GET zůstane bez auth (Grafana funguje dál)
  allow_localhost: true             # localhost (skripty na RPi) bez auth
```

### Krok 2: Restart + ulož bootstrap token

```bash
sudo systemctl restart solarguard
sleep 3
sudo journalctl -u solarguard -n 50 | grep -A 5 "random owner token"
```

Uvidíš:
```
======================================================================
Auth enabled but no users! Generated random owner token:
  T7K2_mB9pXfGhJk_LqRsTuVwXyZaB2cD3eF4gH5iJ6k
Use this token to login as 'owner', then create more users via UI.
Token IS persisted - won't change on next restart.
======================================================================
```

**Zkopíruj ten token a ulož si ho** (Heslový manažer / Bitwarden / poznámky / cokoli).

### Krok 3: Login do PWA

1. Otevři PWA na iPhone (smaž starou z home screen, přidej znovu pro čerstvý cache)
2. Modal "Vlož API token" - vlož bootstrap token
3. **Přihlásit**
4. Vidíš dashboard, vpravo nahoře je pill **`👤 owner`**

### Krok 4: Vytvoř další uživatele

PWA → **Více** (⚙) → **👥 Uživatelé** (vidí jen owner)

#### Pro manželku:
1. Klepni **+ Přidat**
2. Jméno: `manzelka` *(nebo `lucka`, ale jen a-Z, 0-9, _, -)*
3. Role: **family**
4. Token: *automaticky vygenerovaný*
5. **Přidat**
6. **Token Modal** se zobrazí → klikni **📋 Kopírovat**
7. Pošli token manželce přes Signal/SMS/email
8. **Zavřít**

Manželka pak na svém telefonu otevře `http://10.0.0.120:8000` (nebo přes Tailscale), v modalu vloží svůj token, a vidí PWA s pillem `👤 manzelka`. Může klikat Heater ON/OFF, scény, vše. Tab Uživatelé **nevidí** (jen owner).

#### Pro hosty:
- Stejný postup, ale role **guest**
- Hosti vidí všechny grafy, ale tlačítka jsou **zašedlá** (read-only)
- Můžeš jim regenerovat token kdykoli (po odjezdu)

### Krok 5: Změna role uživatele

V kartičce kliknout na **dropdown Role** → vybrat → confirm.

### Krok 6: Když někdo ztratí token

V kartičce klepni **🔑** → confirm → token modal → kopírovat → poslat znovu uživateli.

Starý token okamžitě přestane fungovat.

---

## 🚨 Bezpečnostní doporučení

### ⚠ Pozor: GET endpointy bez auth!

Default `auth_read_too: false` znamená že **GET API (status, history, plán) je bez auth**. Důvod: **Grafana to potřebuje** pro live data.

**Pokud máš PWA jen v LAN nebo Tailscale**: OK, jsi v bezpečí.

**Pokud používáš Tailscale Funnel** (veřejné HTTPS): zvaž zapnutí `auth_read_too: true`. Pak ale Grafana přestane fungovat (pokud jí nedáš token).

### Lepší alternativa pro Tailscale Funnel:

1. SolarGuard + Grafana drží **interně na RPi**
2. Tailscale Funnel jen pro **PWA na portu 8000**
3. Auth zapnutá → veřejný internet vidí jen login modal
4. Grafana přístupná jen přes Tailscale tunnel pro tebe (no Funnel)

---

## 📦 Update postup

```bash
sudo systemctl stop solarguard
cd /home/pi
DATE=$(date +%Y%m%d-%H%M)
tar czf solarguard-pred-v413-${DATE}.tar.gz \
  --exclude='solarguard/.venv' --exclude='solarguard/**/__pycache__' solarguard/

# WinSCP - rozbal solarguard-v4.1.3-FULL.zip přímo do /home/pi/solarguard/

sudo chown -R pi:pi /home/pi/solarguard/
sudo systemctl start solarguard
sudo journalctl -u solarguard -f | grep -iE "(auth|user)"
```

V logu pro auth disabled (default):
```
[INFO] auth: API auth disabled
```

Po zapnutí v configu:
```
[INFO] auth: Auth enabled but no users! Generated random owner token:
  T7K2_mB9pXfGhJk...
```

---

## ✅ Test scénáře

### Test 1: Vytvoření uživatele
1. Owner klikne + Přidat
2. Vyplní jméno "Lucka", role "family", token vygenerován
3. Klikne Přidat
4. **Měl bys vidět**: token modal s velkým tokenem, copy button funguje

### Test 2: Login jako family user
1. Otevři novou tabu/incognito
2. Otevři `http://10.0.0.120:8000`
3. Vlož family token
4. **Měl bys vidět**: dashboard, pill `👤 lucka` (FAMILY zelená)
5. **Tab Uživatelé NENÍ** vidět (jen owner)
6. Můžeš klikat Heater ON/OFF

### Test 3: Login jako guest
1. Vlož guest token
2. **Tlačítka zašedlá** (Heater, scény, atd.)
3. Vidíš všechny grafy a data

### Test 4: Změna role
1. Owner klikne v dropdown role pro "lucka" → "guest"
2. Confirm
3. Lucka po refresh vidí zašedlá tlačítka

### Test 5: Self-regen
1. Owner klikne 🔑 na vlastní řádce
2. Warning "regeneruješ vlastní token"
3. Confirm → token modal
4. Klepneš Zavřít
5. **Auto-login**: stránka se refreshne, jsi přihlášen s novým tokenem

### Test 6: Smaž posledního ownera (negative test)
1. Pokus se kliknout 🗑 na řádce ownera když je jen jeden
2. **Tlačítko je disabled** s tooltip "Nelze smazat posledního ownera"
3. Pokud bys to obešel přes API, vrátí HTTP 400

---

## Co dál

Až manželka i hosti budou mít své tokeny a vše bude fungovat:

| Priorita | Co |
|---|---|
| 1️⃣ | **Test logiky vířivky** - pojď otestovat scénáře z v4.1.2 (state drift, spike) |
| 2️⃣ | **Spot ceny** - ověř že čistá cena ukazuje správně |
| 3️⃣ | **Tweaks** podle reálného provozu |
| 4️⃣ | Future features podle priorit |

Pošli mi po nasazení screenshot:
- Tab Uživatelé s 2-3 uživateli
- Login modal s pillem 👤 jméno
