# SolarGuard v4.1 - Multi-user

Rozšíření v3.9 API auth o **3 role + UI správu uživatelů + audit trail**.

## 3 role

| Role | Číst | Ovládat | Spravovat uživatele |
|---|---|---|---|
| **owner** | ✓ | ✓ | ✓ |
| **family** | ✓ | ✓ | ✗ |
| **guest** | ✓ | ✗ | ✗ |

## Co je nové

### 🔐 Multi-user auth modul (`solarguard/auth.py`)

Přepracován z jediného tokenu na databázi uživatelů:
- Tokeny ukládané jako **SHA256 hash** v `/var/log/solarguard/users.json` (chmod 600)
- Plain-text token vidíš jen **jednou** při vytvoření/regeneraci
- Automatická migrace: pokud máš starý `api.token` z v3.9, vytvoří se z něj implicit user `owner`
- Bootstrap: pokud `auth_enabled: true` ale žádní uživatelé, vygeneruje se náhodný token a vypíše do logu

### 👥 Tab Uživatelé v UI (owner-only)

V "Více" rozcestníku přibyla položka **👥 Uživatelé** (vidí jen owner):
- Seznam uživatelů s rolí, datem vytvoření, last_login
- **+ Přidat** - dialog s polem jméno/role/token + tlačítko "Generovat" (random 32-char base64)
- **🔑 Nový token** - regenerace (starý okamžitě přestane fungovat)
- **🗑 Smazat** - s ochranou proti smazání posledního ownera

### 👤 User pill v headeru

Vpravo nahoře vedle stavu se ukáže pill `👤 owner`. Klepnutím **logout**.

### 🛡 Role-based UI

Pokud jsi přihlášený jako **guest**, JS automaticky **zašedí všechna write tlačítka** (Heater ON, scény, temp, atd.). Vidíš všechno, ale nemůžeš nic změnit.

### 📝 Audit trail

`record_event("web_command", ..., actor="manzelka")` - v eventech vidíš **kdo** kliknul. Tab Události nyní ukáže:

```
14:32  web_command  manzelka  →  heater = True  ✓
14:35  scene        host      →  scene heat_now (ZAMÍTNUTO 403)
```

### 🔄 Backwards compatibility

Pokud máš `api.token: "abc..."` z v3.9, automaticky se z něj při startu vytvoří user `owner`. Nemusíš nic dělat ručně.

## API endpointy

| Endpoint | Role | Co dělá |
|---|---|---|
| `GET /api/auth/status` | any | Vrátí auth_enabled, user, role |
| `POST /api/auth/login` | any | Login s tokenem (vrátí cookie) |
| `POST /api/auth/logout` | any | Smaže cookie |
| `GET /api/users` | owner | Seznam uživatelů |
| `POST /api/users` | owner | Vytvoř uživatele |
| `DELETE /api/users/{name}` | owner | Smaž uživatele |
| `PUT /api/users/{name}` | owner | Změň roli |
| `POST /api/users/{name}/regenerate-token` | owner | Nový token (vrátí plain) |

## Soubory

| Soubor | Změna |
|---|---|
| `solarguard/auth.py` | nahradit (multi-user) |
| `solarguard/web.py` | nahradit (8 nových endpointů + UI) |
| `main.py` | nahradit (init s users.json path) |

## Update postup

Viz **`DEPLOY_v4.1_KOMPLET.md`** - kompletní návod od backupu až po Grafanu.

Pokud chceš jen rychlý update auth modulu:

```bash
sudo systemctl stop solarguard

# Nahraj přes WinSCP:
# auth.py → /home/pi/solarguard/solarguard/auth.py
# web.py  → /home/pi/solarguard/solarguard/web.py
# main.py → /home/pi/solarguard/main.py

sudo systemctl start solarguard
sudo journalctl -u solarguard | grep -i auth
```

## Co dál

| Verze | Téma |
|---|---|
| **v4.2** | Spotřebiče - "kdy pustit pračku" smart alerts |
| **v5.0** | Hardware - DS18B20 ve vodě (přesná teplota) |
| **v5.1** | Voice - HomeKit / Google Home integrace |
