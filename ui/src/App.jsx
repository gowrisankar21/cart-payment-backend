import React, { useCallback, useEffect, useState } from "react";
import { api, money, newKey, setCallLogger } from "./api.js";

const errText = (e) =>
  `${e.code}: ${e.message}` +
  (e.details && Object.keys(e.details).length ? `  ${JSON.stringify(e.details)}` : "");

export default function App() {
  const [tab, setTab] = useState("shop");
  const [products, setProducts] = useState([]);
  const [cart, setCart] = useState(null);
  const [order, setOrder] = useState(null);
  const [notice, setNotice] = useState(null); // {type:'ok'|'err', text}
  const [log, setLog] = useState([]);
  // Idempotency key is kept until checkout succeeds, so "Retry" really re-sends the same key.
  const [idemKey, setIdemKey] = useState(newKey());

  useEffect(() => setCallLogger((c) => setLog((l) => [c, ...l].slice(0, 30))), []);

  const ok = (text) => setNotice({ type: "ok", text });
  const fail = (e) => setNotice({ type: "err", text: errText(e) });
  const run = async (fn) => {
    try {
      return await fn();
    } catch (e) {
      fail(e);
    }
  };

  const loadProducts = useCallback(() => run(async () => setProducts((await api("GET", "/products")).data)), []);
  useEffect(() => {
    loadProducts();
    const saved = localStorage.getItem("cartId");
    if (saved) run(async () => setCart((await api("GET", `/carts/${saved}`)).data)).then(() => {});
  }, [loadProducts]);

  const newCart = () =>
    run(async () => {
      const { data } = await api("POST", "/carts");
      localStorage.setItem("cartId", data.id);
      setCart(data);
      setOrder(null);
      setIdemKey(newKey());
      ok("New cart created.");
    });

  const addToCart = (productId, quantity) =>
    run(async () => {
      let c = cart;
      if (!c || c.status !== "OPEN") {
        c = (await api("POST", "/carts")).data;
        localStorage.setItem("cartId", c.id);
        setIdemKey(newKey());
      }
      const { data } = await api("POST", `/carts/${c.id}/items`, { body: { product_id: productId, quantity } });
      setCart(data);
      ok("Added to cart.");
    });

  return (
    <div className="app">
      <header>
        <h1>Checkout &amp; Rewards demo</h1>
        <nav>
          {["shop", "cart", "admin"].map((t) => (
            <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
              {t === "shop" ? "Shop" : t === "cart" ? `Cart${cart?.items?.length ? ` (${cart.items.length})` : ""}` : "Admin"}
            </button>
          ))}
        </nav>
      </header>

      {notice && (
        <div className={`notice ${notice.type}`} onClick={() => setNotice(null)}>
          {notice.text} <span className="x">✕</span>
        </div>
      )}

      <main>
        {tab === "shop" && <Shop products={products} onAdd={addToCart} onRefresh={loadProducts} />}
        {tab === "cart" && (
          <CartView
            cart={cart}
            order={order}
            idemKey={idemKey}
            setIdemKey={setIdemKey}
            setCart={setCart}
            setOrder={setOrder}
            newCart={newCart}
            ok={ok}
            run={run}
            reloadProducts={loadProducts}
          />
        )}
        {tab === "admin" && <Admin ok={ok} run={run} reloadProducts={loadProducts} />}
      </main>

      <ApiLog log={log} clear={() => setLog([])} />
    </div>
  );
}

/* ----------------------------------------------------------------- Shop */
function Shop({ products, onAdd, onRefresh }) {
  const [qty, setQty] = useState({});
  return (
    <section>
      <div className="row">
        <h2>Products</h2>
        <button onClick={onRefresh}>Refresh</button>
      </div>
      <table>
        <thead>
          <tr><th>ID</th><th>Name</th><th>Price</th><th>In stock</th><th>Qty</th><th /></tr>
        </thead>
        <tbody>
          {products.map((p) => (
            <tr key={p.id}>
              <td>{p.id}</td>
              <td>{p.name}</td>
              <td>{money(p.price_cents)}</td>
              <td className={p.inventory <= 3 ? "warn" : ""}>{p.inventory}</td>
              <td>
                <input type="number" min="1" value={qty[p.id] ?? 1} className="qty"
                  onChange={(e) => setQty({ ...qty, [p.id]: e.target.value })} />
              </td>
              <td><button className="primary" onClick={() => onAdd(p.id, Number(qty[p.id] ?? 1))}>Add to cart</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="hint">Tip: enter 0, a negative number or more than the stock to see validation errors.</p>
    </section>
  );
}

/* ----------------------------------------------------------------- Cart */
function CartView({ cart, order, idemKey, setIdemKey, setCart, setOrder, newCart, ok, run, reloadProducts }) {
  const [coupon, setCoupon] = useState("");
  const [lastReplay, setLastReplay] = useState(false);

  const refresh = () => run(async () => setCart((await api("GET", `/carts/${cart.id}`)).data));
  const update = (pid, q) => run(async () => setCart((await api("PUT", `/carts/${cart.id}/items/${pid}`, { body: { quantity: Number(q) } })).data));
  const remove = (pid) => run(async () => setCart((await api("DELETE", `/carts/${cart.id}/items/${pid}`)).data));

  const checkout = () =>
    run(async () => {
      const { data, replayed } = await api("POST", `/carts/${cart.id}/checkout`, {
        headers: { "Idempotency-Key": idemKey },
        body: coupon.trim() ? { coupon_code: coupon.trim() } : {},
      });
      setOrder(data);
      setLastReplay(replayed);
      setCart({ ...cart, status: "CHECKED_OUT" });
      ok(replayed ? "Replayed the ORIGINAL order (no new order, no extra stock used)." : "Order placed!");
      reloadProducts();
    });

  if (!cart) {
    return (
      <section>
        <p>No cart yet.</p>
        <button className="primary" onClick={newCart}>Create cart</button>
      </section>
    );
  }

  return (
    <section>
      <div className="row">
        <h2>Cart <code className="small">{cart.id}</code> <span className={`badge ${cart.status}`}>{cart.status}</span></h2>
        <div>
          <button onClick={refresh}>Reload</button> <button onClick={newCart}>New cart</button>
        </div>
      </div>

      {cart.items.length === 0 ? <p className="hint">Cart is empty. Add products from the Shop tab.</p> : (
        <table>
          <thead><tr><th>Product</th><th>Unit</th><th>Qty</th><th>Line</th><th>Stock</th><th /></tr></thead>
          <tbody>
            {cart.items.map((i) => (
              <CartRow key={i.product_id} item={i} open={cart.status === "OPEN"} onUpdate={update} onRemove={remove} />
            ))}
          </tbody>
          <tfoot><tr><td colSpan="3">Subtotal</td><td colSpan="3"><b>{money(cart.subtotal_cents)}</b></td></tr></tfoot>
        </table>
      )}

      {cart.status === "OPEN" && cart.items.length > 0 && (
        <div className="panel">
          <h3>Checkout</h3>
          <label>Coupon code (optional)
            <input value={coupon} onChange={(e) => setCoupon(e.target.value)} placeholder="RWD-XXXXXXXXXXXX" />
          </label>
          <label>Idempotency-Key
            <div className="row">
              <input value={idemKey} onChange={(e) => setIdemKey(e.target.value)} className="wide" />
              <button onClick={() => setIdemKey(newKey())}>New key</button>
            </div>
          </label>
          <button className="primary" onClick={checkout}>Place order</button>
          <p className="hint">The key stays the same until an order succeeds, so clicking “Place order” again after a failure or timeout is a safe retry.</p>
        </div>
      )}

      {order && (
        <div className="panel">
          <h3>Order <code className="small">{order.id}</code>{lastReplay && <span className="badge REPLAY">replayed</span>}</h3>
          <table>
            <thead><tr><th>Product</th><th>Unit</th><th>Qty</th><th>Line</th></tr></thead>
            <tbody>
              {order.items.map((i) => (
                <tr key={i.product_id}><td>{i.product_name}</td><td>{money(i.unit_price_cents)}</td><td>{i.quantity}</td><td>{money(i.line_total_cents)}</td></tr>
              ))}
            </tbody>
          </table>
          <p>Gross <b>{money(order.gross_cents)}</b> · Discount{order.discount_percent ? ` (${order.discount_percent}% – ${order.coupon_code})` : ""} <b>-{money(order.discount_cents)}</b> · Net <b>{money(order.net_cents)}</b></p>
          <div className="row">
            <button onClick={checkout}>Retry same request (same key)</button>
            <button onClick={newCart}>Start a new cart</button>
          </div>
        </div>
      )}
    </section>
  );
}

function CartRow({ item, open, onUpdate, onRemove }) {
  const [q, setQ] = useState(item.quantity);
  useEffect(() => setQ(item.quantity), [item.quantity]);
  return (
    <tr>
      <td>{item.name}</td>
      <td>{money(item.unit_price_cents)}</td>
      <td>
        {open ? (<>
          <input type="number" className="qty" value={q} onChange={(e) => setQ(e.target.value)} />{" "}
          <button onClick={() => onUpdate(item.product_id, q)}>Update</button>
        </>) : item.quantity}
      </td>
      <td>{money(item.line_total_cents)}</td>
      <td className={item.in_stock ? "" : "warn"}>{item.available_inventory}{item.in_stock ? "" : " (short!)"}</td>
      <td>{open && <button onClick={() => onRemove(item.product_id)}>Remove</button>}</td>
    </tr>
  );
}

/* ---------------------------------------------------------------- Admin */
function Admin({ ok, run, reloadProducts }) {
  const [report, setReport] = useState(null);
  const [coupons, setCoupons] = useState([]);
  const [orders, setOrders] = useState([]);

  const load = useCallback(() =>
    run(async () => {
      setReport((await api("GET", "/admin/reports/summary")).data);
      setCoupons((await api("GET", "/admin/coupons")).data);
      setOrders((await api("GET", "/admin/orders?limit=50")).data);
    }), []);
  useEffect(() => { load(); }, [load]);

  const generate = () =>
    run(async () => {
      const { data } = await api("POST", "/admin/coupons");
      ok(`Coupon generated: ${data.code} (${data.discount_percent}% off, milestone ${data.milestone_order_count})`);
      load();
    }).then(() => load());

  return (
    <section>
      <div className="banner">ADMIN area – unauthenticated in this demo (the API treats /admin/* as privileged).</div>
      <div className="row">
        <h2>Rewards &amp; report</h2>
        <div><button className="primary" onClick={generate}>Generate coupon</button> <button onClick={() => { load(); reloadProducts(); }}>Refresh</button></div>
      </div>

      {report && (
        <>
          <div className="cards">
            <Card label="Orders" value={report.total_orders} />
            <Card label="Gross revenue" value={money(report.gross_revenue_cents)} />
            <Card label="Discounts" value={money(report.total_discounts_cents)} />
            <Card label="Net revenue" value={money(report.net_revenue_cents)} />
            <Card label="Coupons generated" value={report.coupons.generated} />
            <Card label="Available" value={report.coupons.available} />
            <Card label="Redeemed" value={report.coupons.redeemed} />
          </div>
          <h3>Quantity sold by product</h3>
          {report.quantity_by_product.length === 0 ? <p className="hint">No orders yet.</p> : (
            <table><thead><tr><th>ID</th><th>Product</th><th>Qty sold</th></tr></thead>
              <tbody>{report.quantity_by_product.map((p) => <tr key={p.product_id}><td>{p.product_id}</td><td>{p.name}</td><td>{p.quantity_sold}</td></tr>)}</tbody></table>
          )}
        </>
      )}

      <h3>Coupons</h3>
      {coupons.length === 0 ? <p className="hint">None yet. A coupon becomes available after every nth successful order.</p> : (
        <table><thead><tr><th>Code</th><th>Milestone (order #)</th><th>Discount</th><th>Status</th></tr></thead>
          <tbody>{coupons.map((c) => (
            <tr key={c.id}><td><code>{c.code}</code></td><td>{c.milestone_order_count}</td><td>{c.discount_percent}%</td>
              <td><span className={`badge ${c.status}`}>{c.status}</span></td></tr>))}</tbody></table>
      )}

      <h3>Orders</h3>
      {orders.length === 0 ? <p className="hint">No orders.</p> : (
        <table><thead><tr><th>Order</th><th>Items</th><th>Gross</th><th>Discount</th><th>Net</th><th>Coupon</th></tr></thead>
          <tbody>{orders.map((o) => (
            <tr key={o.id}><td><code className="small">{o.id.slice(0, 8)}</code></td>
              <td>{o.items.map((i) => `${i.quantity}× ${i.product_name}`).join(", ")}</td>
              <td>{money(o.gross_cents)}</td><td>{money(o.discount_cents)}</td><td>{money(o.net_cents)}</td><td>{o.coupon_code ?? "-"}</td></tr>))}</tbody></table>
      )}
    </section>
  );
}

const Card = ({ label, value }) => (<div className="card"><div className="label">{label}</div><div className="value">{value}</div></div>);

/* -------------------------------------------------------------- API log */
function ApiLog({ log, clear }) {
  const [open, setOpen] = useState(true);
  return (
    <footer>
      <div className="row">
        <b onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>{open ? "▼" : "▶"} API calls ({log.length})</b>
        <button onClick={clear}>Clear</button>
      </div>
      {open && (
        <div className="log">
          {log.map((c, i) => (
            <details key={i}>
              <summary>
                <span className={`status s${String(c.status)[0]}`}>{c.status}</span> {c.method} {c.path}
                {c.replayed && <span className="badge REPLAY">Idempotent-Replayed</span>}
                <span className="muted"> {c.ms}ms · {c.at}</span>
              </summary>
              <pre>{JSON.stringify({ requestHeaders: c.requestHeaders, requestBody: c.requestBody, response: c.data }, null, 2)}</pre>
            </details>
          ))}
        </div>
      )}
    </footer>
  );
}
