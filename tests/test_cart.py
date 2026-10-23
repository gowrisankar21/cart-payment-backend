import uuid

from tests.conftest import err, make_cart, run_concurrently, scalar, set_price


def test_health_endpoints(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_request_id_header_is_echoed_and_in_errors(client):
    r = client.get(f"/carts/{uuid.uuid4()}", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    assert err(r)["request_id"] == "abc-123"


def test_list_products(client):
    r = client.get("/products")
    assert r.status_code == 200
    assert len(r.json()) >= 5


def test_cart_crud_with_totals(client):
    cart_id = make_cart(client)
    r = client.get(f"/carts/{cart_id}")
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["subtotal_cents"] == 0

    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 1, "quantity": 2})
    assert r.status_code == 201
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 2, "quantity": 1})
    body = r.json()
    assert [i["product_id"] for i in body["items"]] == [1, 2]
    assert body["items"][0]["line_total_cents"] == 2000
    assert body["items"][1]["unit_price_cents"] == 2550
    assert body["subtotal_cents"] == 2000 + 2550
    assert body["status"] == "OPEN" and body["currency"] == "USD"

    r = client.put(f"/carts/{cart_id}/items/1", json={"quantity": 5})
    assert r.status_code == 200
    assert r.json()["subtotal_cents"] == 5000 + 2550

    r = client.delete(f"/carts/{cart_id}/items/1")
    assert r.status_code == 200
    assert [i["product_id"] for i in r.json()["items"]] == [2]
    assert r.json()["subtotal_cents"] == 2550


def test_invalid_quantities_rejected_and_not_stored(client, db):
    cart_id = make_cart(client)
    for bad in (0, -1, 101, 1.5, "2", True, None):
        r = client.post(f"/carts/{cart_id}/items", json={"product_id": 1, "quantity": bad})
        assert r.status_code == 422, (bad, r.text)
        assert err(r)["code"] == "VALIDATION_ERROR"
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 1})  # missing field
    assert r.status_code == 422
    assert scalar(db, "SELECT count(*) FROM cart_items") == 0


def test_update_with_invalid_quantity_rejected(client):
    cart_id = make_cart(client, {1: 2})
    for bad in (0, -3, 101):
        r = client.put(f"/carts/{cart_id}/items/1", json={"quantity": bad})
        assert r.status_code == 422 and err(r)["code"] == "VALIDATION_ERROR"
    assert client.get(f"/carts/{cart_id}").json()["items"][0]["quantity"] == 2


def test_unknown_product_and_cart_errors(client):
    cart_id = make_cart(client)
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 999, "quantity": 1})
    assert r.status_code == 422 and err(r)["code"] == "PRODUCT_NOT_FOUND"

    missing = uuid.uuid4()
    assert err(client.get(f"/carts/{missing}"))["code"] == "CART_NOT_FOUND"
    r = client.post(f"/carts/{missing}/items", json={"product_id": 1, "quantity": 1})
    assert r.status_code == 404 and err(r)["code"] == "CART_NOT_FOUND"
    assert client.get("/carts/not-a-uuid").status_code == 422


def test_duplicate_add_conflicts_and_missing_item(client):
    cart_id = make_cart(client, {1: 1})
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 1, "quantity": 1})
    assert r.status_code == 409 and err(r)["code"] == "ITEM_ALREADY_IN_CART"
    r = client.put(f"/carts/{cart_id}/items/2", json={"quantity": 1})
    assert r.status_code == 404 and err(r)["code"] == "ITEM_NOT_IN_CART"
    r = client.delete(f"/carts/{cart_id}/items/2")
    assert r.status_code == 404 and err(r)["code"] == "ITEM_NOT_IN_CART"


def test_quantity_above_available_inventory_rejected(client):
    cart_id = make_cart(client)
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 6, "quantity": 2})
    assert r.status_code == 409 and err(r)["code"] == "INSUFFICIENT_STOCK"
    assert err(r)["details"]["items"][0] == {"product_id": 6, "requested": 2, "available": 1}


def test_price_change_after_add_is_reflected_in_cart(client, db):
    cart_id = make_cart(client, {1: 2})
    set_price(db, 1, 1500)
    body = client.get(f"/carts/{cart_id}").json()
    assert body["items"][0]["unit_price_cents"] == 1500
    assert body["subtotal_cents"] == 3000


def test_concurrent_adds_of_same_product_yield_one_row(app, client, db):
    cart_id = make_cart(client)

    def add(c):
        return c.post(f"/carts/{cart_id}/items", json={"product_id": 1, "quantity": 1})

    results = run_concurrently(app, [add, add, add, add])
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409]
    assert scalar(db, "SELECT count(*) FROM cart_items WHERE cart_id = :c", c=cart_id) == 1
