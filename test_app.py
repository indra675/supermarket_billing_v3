"""Run with:  python -m unittest -v"""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "test-secret"

import app as counter  # noqa: E402


def item(pid, qty, **kw):
    return {"productId": pid, "name": kw.get("name", "x"), "price": kw.get("price", 0),
            "unit": "kg", "quantity": qty, "gstRate": kw.get("gst", 0)}


def bill(items, **kw):
    return {"customerName": "Test", "items": items, **kw}


class CounterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(counter.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        counter.init_db()

    def setUp(self):
        counter._failed.clear()
        self.admin = counter.app.test_client()
        self.cashier = counter.app.test_client()
        self.anon = counter.app.test_client()
        conn = counter.get_db()
        for t in ("stock_movements", "bill_items", "bills", "products", "users"):
            conn.execute(f"DELETE FROM {t}")
        conn.close()
        # first-run setup creates the admin and signs them in
        r = self.admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass1"})
        self.assertEqual(r.status_code, 201)
        r = self.admin.post("/api/users", json={"username": "ravi", "password": "ravipass1", "role": "cashier"})
        self.assertEqual(r.status_code, 201)
        r = self.cashier.post("/api/auth/login", json={"username": "ravi", "password": "ravipass1"})
        self.assertEqual(r.status_code, 200)

    def make_product(self, name="Rice", price=60, stock=10, reorder=2, gst=5, sku=None):
        r = self.admin.post("/api/products", json={
            "name": name, "price": price, "unit": "kg", "gstRate": gst,
            "stock": stock, "reorderLevel": reorder, "sku": sku})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def stock_of(self, pid):
        return [p for p in self.admin.get("/api/products").get_json() if p["id"] == pid][0]["stock"]

    # ---------- auth ----------
    def test_setup_only_once(self):
        r = self.anon.post("/api/auth/setup", json={"username": "evil", "password": "evilpass1"})
        self.assertEqual(r.status_code, 403)

    def test_api_requires_login(self):
        for url in ("/api/bills", "/api/products", "/api/stats", "/api/users"):
            self.assertEqual(self.anon.get(url).status_code, 401, url)

    def test_cashier_cannot_use_admin_endpoints(self):
        p = self.make_product()
        self.assertEqual(self.cashier.get("/api/stats").status_code, 403)
        self.assertEqual(self.cashier.get("/api/users").status_code, 403)
        self.assertEqual(self.cashier.post("/api/products", json={"name": "a", "price": 1}).status_code, 403)
        self.assertEqual(self.cashier.post(f"/api/products/{p['id']}/stock", json={"change": 5}).status_code, 403)
        self.assertEqual(self.cashier.get("/api/bills/export.csv").status_code, 403)

    def test_password_rules_and_hashing(self):
        r = self.admin.post("/api/users", json={"username": "bob", "password": "short", "role": "cashier"})
        self.assertEqual(r.status_code, 400)
        conn = counter.get_db()
        h = conn.execute("SELECT password_hash FROM users WHERE username='boss'").fetchone()[0]
        conn.close()
        self.assertNotIn("bosspass1", h)

    def test_wrong_password_and_lockout(self):
        for _ in range(counter.MAX_FAILED_LOGINS):
            r = self.anon.post("/api/auth/login", json={"username": "ravi", "password": "nope"})
            self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/auth/login", json={"username": "ravi", "password": "ravipass1"})
        self.assertEqual(r.status_code, 429)  # locked even with the right password

    def test_deactivated_cashier_is_kicked_out(self):
        uid = [u for u in self.admin.get("/api/users").get_json() if u["username"] == "ravi"][0]["id"]
        self.admin.put(f"/api/users/{uid}", json={"active": False})
        self.assertEqual(self.cashier.get("/api/bills").status_code, 401)
        r = self.anon.post("/api/auth/login", json={"username": "ravi", "password": "ravipass1"})
        self.assertEqual(r.status_code, 401)

    def test_admin_cannot_lock_self_out(self):
        me = self.admin.get("/api/auth/me").get_json()["user"]["id"]
        self.assertEqual(self.admin.put(f"/api/users/{me}", json={"active": False}).status_code, 400)
        self.assertEqual(self.admin.put(f"/api/users/{me}", json={"role": "cashier"}).status_code, 400)

    def test_change_password(self):
        r = self.cashier.post("/api/auth/password", json={"current": "wrongwrong", "new": "newpass123"})
        self.assertEqual(r.status_code, 400)
        r = self.cashier.post("/api/auth/password", json={"current": "ravipass1", "new": "newpass123"})
        self.assertEqual(r.status_code, 200)
        r = self.anon.post("/api/auth/login", json={"username": "RAVI", "password": "newpass123"})
        self.assertEqual(r.status_code, 200)  # usernames are case-insensitive

    # ---------- bills & roles ----------
    def test_cashier_sees_only_own_bills_and_cannot_edit_or_delete(self):
        p = self.make_product()
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 1)]))
        self.assertEqual(r.status_code, 201)
        bid = r.get_json()["id"]
        self.assertEqual(r.get_json()["cashierName"], "ravi")
        self.admin.post("/api/bills", json=bill([item(p["id"], 1)]))
        self.assertEqual(len(self.cashier.get("/api/bills").get_json()), 1)
        self.assertEqual(len(self.admin.get("/api/bills").get_json()), 2)
        self.assertEqual(self.cashier.put(f"/api/bills/{bid}", json=bill([item(p["id"], 2)])).status_code, 403)
        self.assertEqual(self.cashier.delete(f"/api/bills/{bid}").status_code, 403)

    def test_cashier_cannot_open_someone_elses_bill(self):
        p = self.make_product()
        other = self.admin.post("/api/bills", json=bill([item(p["id"], 1)])).get_json()["id"]
        self.assertEqual(self.cashier.get(f"/api/bills/{other}").status_code, 404)

    def test_cashier_cannot_override_catalog_price(self):
        p = self.make_product(price=60, gst=5)
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 2, price=1, gst=0)]))
        b = r.get_json()
        self.assertEqual(b["items"][0]["price"], 60)
        self.assertEqual(b["subtotal"], 120)
        self.assertEqual(b["tax"], 6)

    def test_admin_may_override_price(self):
        p = self.make_product(price=60)
        b = self.admin.post("/api/bills", json=bill([item(p["id"], 1, price=50)])).get_json()
        self.assertEqual(b["items"][0]["price"], 50)

    def test_cashier_discount_cap(self):
        p = self.make_product()
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 1)], discountType="percent", discountValue=25))
        self.assertEqual(r.status_code, 400)
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 1)], discountType="percent", discountValue=10))
        self.assertEqual(r.status_code, 201)
        r = self.admin.post("/api/bills", json=bill([item(p["id"], 1)], discountType="percent", discountValue=25))
        self.assertEqual(r.status_code, 201)

    def test_totals_with_gst_and_discount(self):
        p = self.make_product(price=100, gst=18, stock=None)
        b = self.admin.post("/api/bills", json=bill([item(p["id"], 2, price=100, gst=18)], discountType="percent", discountValue=10)).get_json()
        # subtotal 200, discount 20, GST 18% of 180 = 32.40, total 212.40
        self.assertEqual((b["subtotal"], b["discountAmount"], b["tax"], b["total"]), (200, 20, 32.4, 212.4))

    # ---------- stock ----------
    def test_sale_deducts_stock_and_logs(self):
        p = self.make_product(stock=10)
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 3)]))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.stock_of(p["id"]), 7)
        moves = self.admin.get(f"/api/products/{p['id']}/movements").get_json()
        self.assertEqual([m["reason"] for m in moves], ["sale", "opening"])
        self.assertEqual(moves[0]["change"], -3)
        self.assertEqual(moves[0]["username"], "ravi")

    def test_cannot_oversell(self):
        p = self.make_product(stock=2)
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 3)]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("only 2", r.get_json()["error"])
        self.assertEqual(self.stock_of(p["id"]), 2)
        self.assertEqual(len(self.admin.get("/api/bills").get_json()), 0)  # rolled back, no half-saved bill

    def test_oversell_across_lines_of_same_product(self):
        p = self.make_product(stock=5)
        r = self.cashier.post("/api/bills", json=bill([item(p["id"], 3), item(p["id"], 3)]))
        self.assertEqual(r.status_code, 400)

    def test_failed_bill_rolls_back_all_stock(self):
        a = self.make_product("A", stock=10, sku="a")
        b = self.make_product("B", stock=1, sku="b")
        r = self.cashier.post("/api/bills", json=bill([item(a["id"], 4), item(b["id"], 5)]))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock_of(a["id"]), 10)

    def test_edit_bill_adjusts_stock_by_difference(self):
        p = self.make_product(stock=10)
        bid = self.admin.post("/api/bills", json=bill([item(p["id"], 4)])).get_json()["id"]
        self.assertEqual(self.stock_of(p["id"]), 6)
        r = self.admin.put(f"/api/bills/{bid}", json=bill([item(p["id"], 6)]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock_of(p["id"]), 4)
        r = self.admin.put(f"/api/bills/{bid}", json=bill([item(p["id"], 11)]))  # 10 total exist
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock_of(p["id"]), 4)
        r = self.admin.put(f"/api/bills/{bid}", json=bill([item(p["id"], 10)]))  # exactly everything
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock_of(p["id"]), 0)

    def test_delete_bill_restores_stock(self):
        p = self.make_product(stock=10)
        bid = self.cashier.post("/api/bills", json=bill([item(p["id"], 4)])).get_json()["id"]
        self.assertEqual(self.admin.delete(f"/api/bills/{bid}").status_code, 200)
        self.assertEqual(self.stock_of(p["id"]), 10)

    def test_untracked_products_sell_freely(self):
        p = self.make_product(stock=None)
        self.assertEqual(self.cashier.post("/api/bills", json=bill([item(p["id"], 999)])).status_code, 201)
        self.assertIsNone(self.stock_of(p["id"]))

    def test_restock_adjust_and_count(self):
        p = self.make_product(stock=5)
        r = self.admin.post(f"/api/products/{p['id']}/stock", json={"change": 20, "note": "delivery"})
        self.assertEqual(r.get_json()["stock"], 25)
        r = self.admin.post(f"/api/products/{p['id']}/stock", json={"change": -30})
        self.assertEqual(r.status_code, 400)  # can't go negative
        r = self.admin.post(f"/api/products/{p['id']}/stock", json={"set": 22, "note": "counted"})
        self.assertEqual(r.get_json()["stock"], 22)
        reasons = [m["reason"] for m in self.admin.get(f"/api/products/{p['id']}/movements").get_json()]
        self.assertEqual(reasons, ["count", "restock", "opening"])

    def test_start_tracking_an_untracked_product(self):
        p = self.make_product(stock=None)
        r = self.admin.post(f"/api/products/{p['id']}/stock", json={"set": 12})
        self.assertEqual(r.get_json()["stock"], 12)

    def test_product_edit_cannot_change_stock(self):
        p = self.make_product(stock=5)
        r = self.admin.put(f"/api/products/{p['id']}", json={"name": "Rice", "price": 70, "stock": 999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock_of(p["id"]), 5)

    def test_low_stock_flag_and_dashboard(self):
        p = self.make_product(stock=3, reorder=5)
        self.assertTrue([x for x in self.admin.get("/api/products").get_json() if x["id"] == p["id"]][0]["lowStock"])
        s = self.admin.get("/api/stats").get_json()
        self.assertEqual(s["lowStock"][0]["name"], "Rice")

    def test_stats_by_cashier(self):
        p = self.make_product(stock=None, price=100, gst=0)
        self.cashier.post("/api/bills", json=bill([item(p["id"], 1)]))
        s = self.admin.get("/api/stats").get_json()
        self.assertEqual(s["cashiers"][0]["name"], "ravi")
        self.assertEqual(s["cashiers"][0]["sales"], 100)

    def test_deleting_product_keeps_old_bills(self):
        p = self.make_product(stock=10)
        self.cashier.post("/api/bills", json=bill([item(p["id"], 1)]))
        self.assertEqual(self.admin.delete(f"/api/products/{p['id']}").status_code, 200)
        b = self.admin.get("/api/bills").get_json()[0]
        self.assertIsNone(b["items"][0]["productId"])

    def test_search_is_literal_and_scoped(self):
        p = self.make_product(stock=None)
        self.cashier.post("/api/bills", json={**bill([item(p["id"], 1)]), "customerName": "100% Sharma"})
        self.assertEqual(len(self.cashier.get("/api/bills?q=100%25").get_json()), 1)
        self.assertEqual(len(self.cashier.get("/api/bills?q=%25").get_json()), 1)  # % is a literal, not a wildcard
        self.assertEqual(len(self.cashier.get("/api/bills?q=zzz").get_json()), 0)


if __name__ == "__main__":
    unittest.main()
