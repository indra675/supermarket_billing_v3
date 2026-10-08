"""Optional: fill the catalog with demo products (with stock) so you can try
barcode billing and low-stock alerts straight away.

    python seed_demo.py

Safe to re-run: it only inserts when the products table is empty."""
import app

DEMO = [  # name, sku, price, unit, gst%, stock, reorder at
    ("Basmati Rice", "8901001", 95, "kg", 5, 50, 10),
    ("Toor Dal", "8901002", 140, "kg", 5, 30, 8),
    ("Sunflower Oil", "8901003", 165, "L", 5, 24, 6),
    ("Sugar", "8901004", 46, "kg", 5, 40, 10),
    ("Amul Butter 100g", "8901005", 58, "pcs", 12, 36, 12),
    ("Milk 500ml", "8901006", 29, "pcs", 0, 60, 20),
    ("Parle-G Biscuits", "8901007", 10, "pcs", 18, 120, 30),
    ("Dishwash Liquid", "8901008", 110, "pcs", 18, 3, 5),   # starts below reorder -> low-stock alert
    ("Bath Soap", "8901009", 38, "pcs", 18, 45, 10),
    ("Tea 250g", "8901010", 130, "pcs", 5, 18, 6),
]

conn = app.get_db()
if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
    print("Catalog already has products - nothing to do.")
else:
    now = app.now_iso()
    for name, sku, price, unit, gst, stock, reorder in DEMO:
        cur = conn.execute(
            "INSERT INTO products (name, sku, price, unit, gst_rate, stock, reorder_level) VALUES (?,?,?,?,?,?,?)",
            (name, sku, price, unit, gst, stock, reorder))
        conn.execute(
            "INSERT INTO stock_movements (product_id, change, balance, reason, username, created_at) "
            "VALUES (?,?,?,?,?,?)", (cur.lastrowid, stock, stock, "opening", "seed", now))
    print(f"Added {len(DEMO)} demo products.")
conn.close()
