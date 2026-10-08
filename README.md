# 🧾 Counter — Supermarket Billing (v3)

> A multi-user billing system for a supermarket counter: cashier logins, role-based permissions, live stock tracking with an audit trail, GST, discounts and a sales dashboard. Flask + SQLite + vanilla JS.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-black?logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-07405e?logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/tests-28_passing-4c7a3f)

## ✨ Features

**Billing** — live receipt, decimal quantities (0.5 kg), per-item GST, % or flat discount, Cash/UPI/Card, numbered bills (`B-00001`), barcode/SKU lookup, search, print/PDF, `.txt` and CSV export.

**🔐 Accounts & roles (new in v3)**
- First launch asks you to create the **admin** account; after that, sign-in is required for everything.
- Admins create **cashier** accounts, reset passwords, promote/demote, and disable accounts (a disabled cashier is signed out immediately).
- Passwords are hashed (scrypt via Werkzeug); sessions use HttpOnly + SameSite cookies; 5 failed logins lock that username for 5 minutes.

| | Cashier | Admin |
|---|---|---|
| Create bills | ✅ | ✅ |
| See bill history | own bills only | all bills |
| Edit / delete saved bills | ❌ (print only) | ✅ |
| Change catalog prices | ❌ | ✅ |
| Override a catalog item's price on a bill | ❌ (server uses catalog price) | ✅ |
| Discount | up to 10% (configurable) | any |
| Products, stock, dashboard, team | ❌ | ✅ |

**📦 Stock tracking (new in v3)**
- Per-product stock with a **reorder level**; leave stock blank to not track a product.
- Every sale deducts stock. **Overselling is blocked** (checked in the browser *and* on the server).
- Editing a bill adjusts stock by the difference; deleting a bill puts the items back.
- Stock can only change through the **ledger** (`stock_movements`): sale, opening stock, received, write-off, physical count, bill edit/delete — each row records who, when, why and the resulting balance.
- Dashboard shows **low-stock alerts** and **sales per cashier**.
- Bills are saved in one database transaction (`BEGIN IMMEDIATE`), so two cashiers selling the last packet at the same moment can't both succeed, and a failed bill never leaves half-updated stock.

## 🚀 Run it

```bash
python -m venv venv
venv\Scripts\activate          # Windows   (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** → create the admin account → add products (or run `python seed_demo.py` first for 10 sample products with stock).

Upgrading from v2? Copy your old `billing.db` next to `app.py` — it migrates itself on first start. Existing products start as "not tracked", so nothing stops selling.

## ⚙️ Configuration (environment variables, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | auto-generated `.secret_key` file | Signs session cookies. **Set it in production.** |
| `COOKIE_SECURE` | off | Set to `1` when served over HTTPS |
| `TRUST_PROXY` | off | Set to `1` behind Render/Heroku so lockouts use the real client IP |
| `MAX_CASHIER_DISCOUNT_PCT` | `10` | Highest discount a cashier may give |
| `CASHIER_CUSTOM_ITEMS` | `1` | Set `0` to force cashiers to bill catalog items only |
| `DB_PATH` | `./billing.db` | Database location |
| `FLASK_DEBUG` | off | `1` enables auto-reload while developing |

## 🧪 Tests

```bash
python -m unittest -v
```

28 tests cover first-run setup, login lockout, role permissions, price/discount enforcement, oversell protection, rollback on failed bills, stock changes on edit/delete, the ledger, and search.

## 🗂️ Project structure

```
├── app.py            # Flask API: auth, bills, products, stock ledger, stats
├── test_app.py       # unit/integration tests
├── seed_demo.py      # optional demo catalog
├── templates/index.html
└── static/{app.js, style.css}
```

## 📡 API (all routes need a session; ★ = admin only)

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/auth/me` | Who am I / is first-run setup needed |
| `POST` | `/api/auth/setup` | Create the first admin (only works once) |
| `POST` | `/api/auth/login` · `/logout` · `/password` | Session handling |
| `GET/POST` | `/api/bills` | List (own bills for cashiers) / create |
| `GET` | `/api/bills/<id>` | One bill |
| `PUT/DELETE` ★ | `/api/bills/<id>` | Edit / delete (stock adjusts) |
| `GET` ★ | `/api/bills/export.csv` | All bills as CSV |
| `GET` | `/api/products` | Catalog with stock |
| `POST/PUT/DELETE` ★ | `/api/products[/<id>]` | Manage catalog |
| `POST` ★ | `/api/products/<id>/stock` | `{"change": 20}` or `{"set": 47}` + `note` |
| `GET` ★ | `/api/products/<id>/movements` | Stock ledger |
| `GET` ★ | `/api/stats` | Dashboard numbers |
| `GET/POST/PUT` ★ | `/api/users[/<id>]` | Team management |

## 🔒 Security notes & limits

- Cashiers can't edit prices of catalog items; for non-catalog items they can type a price (turn that off with `CASHIER_CUSTOM_ITEMS=0`).
- Changing a password doesn't sign out other open sessions on that account (they expire after 12 h); disabling the account does cut it off instantly.
- Login lockout is kept in memory, so it resets when the server restarts.
- Item prices are GST-exclusive; GST is added on top and reduced proportionally when a discount is applied.

## 🛣️ Next steps

- [x] Multi-cashier login with roles
- [x] Stock tracking with audit ledger
- [ ] Purchase orders / supplier records for restocking
- [ ] Returns and refunds (credit notes that restock items)
- [ ] PostgreSQL for hosting (Render's free disk is wiped on redeploy, so SQLite data is lost there)
- [ ] End-of-day cash register close-out per cashier

<p align="center">Built with 🧾 and a bit of ₹.</p>
