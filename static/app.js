// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let me = null;               // {id, username, role}
let config = { maxCashierDiscount: 10, cashierCustomItems: true };
let authMode = 'login';      // 'login' | 'setup'

let currentItems = [];       // {productId, name, price, unit, quantity, gstRate}
let editingBillId = null;
let editingBillNo = null;
let viewOnly = false;        // cashiers can print a saved bill but not change it
let currentBillDate = new Date();
let currentCashier = '';

let products = [];
let matched = null;          // catalog product matched in the "add item" form
let editingProductId = null;
let stockProduct = null;
let pwTarget = null;         // null = my own password, else a user id

const $ = (id) => document.getElementById(id);
const isAdmin = () => me && me.role === 'admin';
const money = (n) => '₹' + Number(n).toFixed(2);
const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100;
const qty = (n) => String(Number(Number(n).toFixed(3)));
const escapeHtml = (str) => {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
};

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 2400);
}

function setHint(el, msg, kind = '') {
  el.textContent = msg;
  el.className = 'hint ' + kind;
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !url.startsWith('/api/auth/')) {
    showAuth(false, 'Your session ended. Please sign in again.');
    throw new Error('Please sign in again.');
  }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------------------
// Sign in / setup / sign out
// ---------------------------------------------------------------------------
function showAuth(needsSetup, message = '') {
  me = null;
  authMode = needsSetup ? 'setup' : 'login';
  $('appRoot').classList.add('hidden');
  $('authScreen').classList.remove('hidden');
  $('authPass2Wrap').classList.toggle('hidden', !needsSetup);
  $('authPass').autocomplete = needsSetup ? 'new-password' : 'current-password';
  $('authTag').textContent = needsSetup ? 'First run — create the admin account.' : 'Sign in to start billing.';
  $('authBtn').textContent = needsSetup ? 'Create admin account' : 'Sign in';
  $('authPass').value = '';
  $('authPass2').value = '';
  setHint($('authStatus'), message, message ? 'error' : '');
  $('authUser').focus();
}

$('authForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('authUser').value.trim();
  const password = $('authPass').value;
  if (authMode === 'setup' && password !== $('authPass2').value) {
    return setHint($('authStatus'), 'Passwords do not match.', 'error');
  }
  try {
    const d = await api(authMode === 'setup' ? '/api/auth/setup' : '/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    config = d.config || config;
    $('authPass').value = '';
    startApp(d.user);
  } catch (err) {
    setHint($('authStatus'), err.message, 'error');
  }
});

$('logoutBtn').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
  resetBill();
  showAuth(false);
});

function startApp(user) {
  me = user;
  $('authScreen').classList.add('hidden');
  $('appRoot').classList.remove('hidden');
  $('whoami').textContent = `${me.username} · ${me.role}`;
  document.querySelectorAll('.admin-only').forEach((el) => el.classList.toggle('hidden', !isAdmin()));
  $('historyTitle').textContent = isAdmin() ? 'History' : 'My bills';
  $('discountHint').textContent = isAdmin() ? '' : `Cashier discount limit: ${config.maxCashierDiscount}%`;
  resetBill();
  switchTab('billing');
}

async function boot() {
  try {
    const s = await fetch('/api/auth/me', { credentials: 'same-origin' }).then((r) => r.json());
    if (s.config) config = s.config;
    if (s.user) startApp(s.user);
    else showAuth(s.needsSetup);
  } catch (err) {
    showAuth(false, 'Cannot reach the server.');
  }
}

// ----- password dialog (own password, or admin resetting someone's) -----
function openPwDialog(targetId = null, username = '') {
  pwTarget = targetId;
  $('pwTitle').textContent = targetId ? `Reset password for ${username}` : 'Change password';
  $('pwCurWrap').classList.toggle('hidden', !!targetId);
  $('pwCur').value = '';
  $('pwNew').value = '';
  setHint($('pwStatus'), '');
  $('pwDialog').showModal();
}
$('pwBtn').addEventListener('click', () => openPwDialog());
$('pwCancel').addEventListener('click', () => $('pwDialog').close());
$('pwSave').addEventListener('click', async () => {
  try {
    if (pwTarget) {
      await api(`/api/users/${pwTarget}`, { method: 'PUT', body: JSON.stringify({ password: $('pwNew').value }) });
    } else {
      await api('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current: $('pwCur').value, new: $('pwNew').value }),
      });
    }
    $('pwDialog').close();
    toast('Password updated');
  } catch (err) {
    setHint($('pwStatus'), err.message, 'error');
  }
});

// ---------------------------------------------------------------------------
// Totals — mirrors the server's maths so the preview matches what is saved
// ---------------------------------------------------------------------------
function computeTotals() {
  let subtotal = 0;
  let lineTax = 0;
  currentItems.forEach((it) => {
    const line = round2(it.price * it.quantity);
    subtotal += line;
    lineTax += (line * it.gstRate) / 100;
  });
  subtotal = round2(subtotal);

  const type = $('discountType').value;
  const val = Math.max(0, parseFloat($('discountValue').value) || 0);
  let discount = type === 'percent' ? round2((subtotal * Math.min(val, 100)) / 100) : round2(val);
  const discountTooBig = discount > subtotal;
  if (discountTooBig) discount = subtotal;

  const ratio = subtotal ? (subtotal - discount) / subtotal : 0;
  const tax = round2(lineTax * ratio);
  const total = round2(subtotal - discount + tax);
  return { subtotal, discount, tax, total, discountTooBig, type, val };
}

// ---------------------------------------------------------------------------
// Receipt rendering
// ---------------------------------------------------------------------------
function renderItems() {
  const body = $('itemsBody');
  body.innerHTML = '';
  $('emptyMsg').style.display = currentItems.length ? 'none' : 'block';

  currentItems.forEach((it, idx) => {
    const row = document.createElement('div');
    row.className = 'receipt-row';
    row.innerHTML = `
      <span class="name">${escapeHtml(it.name)}</span>
      <span class="qty">${qty(it.quantity)}${it.unit ? ' ' + escapeHtml(it.unit) : ''}</span>
      <span class="price">${money(it.price)}</span>
      <span class="total">${money(round2(it.price * it.quantity))}</span>
      ${it.gstRate ? `<span class="gst-note">GST ${it.gstRate}%</span>` : ''}
      ${viewOnly ? '' : `<button class="remove no-print" data-idx="${idx}">remove</button>`}`;
    body.appendChild(row);
  });
  body.querySelectorAll('button[data-idx]').forEach((btn) =>
    btn.addEventListener('click', () => {
      currentItems.splice(Number(btn.dataset.idx), 1);
      renderItems();
    })
  );

  const t = computeTotals();
  $('subtotalVal').textContent = money(t.subtotal);
  $('discountLine').style.display = t.discount > 0 ? 'flex' : 'none';
  $('discountLabel').textContent = t.type === 'percent' ? `Discount (${t.val}%)` : 'Discount';
  $('discountVal').textContent = '−' + money(t.discount);
  $('taxVal').textContent = money(t.tax);
  $('grandTotal').textContent = money(t.total);
  $('billDate').textContent = currentBillDate.toLocaleDateString();
  $('billNo').textContent = editingBillNo || 'NEW';
  $('billPayment').textContent = $('paymentMethod').value;
  $('billCustomerLabel').textContent = $('customerName').value.trim() || '—';
  $('billCashier').textContent = currentCashier || (me ? me.username : '—');
  $('saveBillBtn').disabled = viewOnly;
  $('saveBillBtn').textContent = viewOnly ? 'Saved (print only)' : editingBillId ? 'Update bill' : 'Save bill';
}

['discountValue', 'discountType', 'paymentMethod', 'customerName'].forEach((id) =>
  $(id).addEventListener('input', renderItems)
);

// ---------------------------------------------------------------------------
// Add-item form: catalog lookup, price lock for cashiers, stock hints
// ---------------------------------------------------------------------------
function findProduct(text) {
  const t = text.trim().toLowerCase();
  if (!t) return null;
  return products.find((p) => (p.sku && p.sku.toLowerCase() === t) || p.name.toLowerCase() === t) || null;
}

function usedInBill(productId) {
  return currentItems.filter((i) => i.productId === productId).reduce((s, i) => s + i.quantity, 0);
}

function fillFromProduct(p) {
  $('itemName').value = p.name;
  $('itemPrice').value = p.price;
  $('itemUnit').value = p.unit || '';
  $('itemGst').value = String(p.gstRate);
  if (!$('itemQty').value) $('itemQty').value = 1;
}

function clearAutofill() {
  ['itemPrice', 'itemUnit', 'itemQty'].forEach((id) => ($(id).value = ''));
  $('itemGst').value = '0';
}

function applyLock() {
  // Cashiers can't change a catalog item's price/unit/GST: the catalog decides.
  const lock = !!matched && !isAdmin();
  $('itemPrice').readOnly = lock;
  $('itemUnit').readOnly = lock;
  $('itemGst').disabled = lock;
}

function showStockHint() {
  const el = $('stockHint');
  if (!matched || matched.stock === null) return setHint(el, '');
  const left = matched.stock - (editingBillId ? 0 : usedInBill(matched.id));
  const unit = matched.unit ? ' ' + matched.unit : '';
  if (left <= 0) setHint(el, `Out of stock${editingBillId ? '' : ' (all in this bill)'}.`, 'stock-hint out');
  else if (matched.stock <= matched.reorderLevel) setHint(el, `Low stock: ${qty(left)}${unit} left.`, 'stock-hint low');
  else setHint(el, `In stock: ${qty(left)}${unit}`, 'stock-hint');
}

$('itemName').addEventListener('input', () => {
  const p = findProduct($('itemName').value);
  if (p) {
    if (!matched || matched.id !== p.id) fillFromProduct(p);
    matched = p;
  } else if (matched) {
    matched = null;       // typed away from a catalog item: drop the auto-filled values
    clearAutofill();
  }
  applyLock();
  showStockHint();
});

function addItem() {
  const statusEl = $('addStatus');
  const p = matched;
  const name = p ? p.name : $('itemName').value.trim();
  const price = parseFloat($('itemPrice').value);
  const unit = $('itemUnit').value.trim();
  const q = parseFloat($('itemQty').value || (p ? 1 : ''));
  const gstRate = parseFloat($('itemGst').value) || 0;

  if (!name || isNaN(price) || price < 0 || isNaN(q) || q <= 0) {
    return setHint(statusEl, 'Fill in a valid item name, price, and quantity.', 'error');
  }
  if (!p && !isAdmin() && !config.cashierCustomItems) {
    return setHint(statusEl, 'Pick an item from the catalog (custom items are turned off).', 'error');
  }
  if (p && p.stock !== null && !editingBillId) {
    const left = p.stock - usedInBill(p.id);
    if (q > left + 1e-9) {
      return setHint(statusEl, `Only ${qty(Math.max(left, 0))}${p.unit ? ' ' + p.unit : ''} of "${p.name}" left in stock.`, 'error');
    }
  }

  const pid = p ? p.id : null;
  const dup = currentItems.find(
    (i) => i.productId === pid && i.name.toLowerCase() === name.toLowerCase() &&
           i.price === price && i.unit === unit && i.gstRate === gstRate
  );
  if (dup) dup.quantity = round2(dup.quantity + q);
  else currentItems.push({ productId: pid, name, price, unit, quantity: q, gstRate });

  renderItems();
  setHint(statusEl, `Added "${name}".`, 'ok');
  ['itemName', 'itemPrice', 'itemUnit', 'itemQty'].forEach((id) => ($(id).value = ''));
  $('itemGst').value = '0';
  matched = null;
  applyLock();
  showStockHint();
  $('itemName').focus();
}

$('addItemBtn').addEventListener('click', addItem);
['itemName', 'itemPrice', 'itemUnit', 'itemQty'].forEach((id) =>
  $(id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addItem(); }
  })
);

// ---------------------------------------------------------------------------
// Bill actions
// ---------------------------------------------------------------------------
function resetBill() {
  currentItems = [];
  editingBillId = null;
  editingBillNo = null;
  viewOnly = false;
  currentCashier = '';
  matched = null;
  currentBillDate = new Date();
  ['customerName', 'discountValue', 'itemName', 'itemPrice', 'itemUnit', 'itemQty'].forEach((id) => ($(id).value = ''));
  $('itemGst').value = '0';
  $('discountType').value = 'percent';
  $('paymentMethod').value = 'Cash';
  applyLock();
  showStockHint();
  renderItems();
  setHint($('saveStatus'), '');
  setHint($('addStatus'), '');
}

$('clearBillBtn').addEventListener('click', () => {
  if (currentItems.length && !viewOnly && !confirm('Discard the current bill?')) return;
  resetBill();
});

$('printBtn').addEventListener('click', () => window.print());

$('downloadBtn').addEventListener('click', () => {
  const name = $('customerName').value.trim() || 'Customer';
  const t = computeTotals();
  let text = `Supermarket Bill ${editingBillNo || ''}\n-----------------------------\nCustomer: ${name}\nCashier: ${currentCashier || me.username}\nDate: ${currentBillDate.toLocaleDateString()}\n\nItem\tPrice\tQuantity\tGST\tTotal\n`;
  currentItems.forEach((it) => {
    text += `${it.name}\t₹${it.price.toFixed(2)}\t${qty(it.quantity)} ${it.unit}\t${it.gstRate}%\t₹${round2(it.price * it.quantity).toFixed(2)}\n`;
  });
  text += `-----------------------------\nSubtotal: ₹${t.subtotal.toFixed(2)}\n`;
  if (t.discount > 0) text += `Discount: -₹${t.discount.toFixed(2)}\n`;
  text += `GST: ₹${t.tax.toFixed(2)}\nTotal Cost: ₹${t.total.toFixed(2)}\nPaid by: ${$('paymentMethod').value}\nThank you for shopping with us!\n`;

  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `bill_${name.replace(/\s+/g, '_')}.txt`;
  a.click();
  URL.revokeObjectURL(url);
});

async function saveBill() {
  const statusEl = $('saveStatus');
  if (viewOnly) return setHint(statusEl, 'This bill is saved. Press "New bill" for the next customer.', 'error');
  const name = $('customerName').value.trim();
  if (!name) return setHint(statusEl, 'Enter a customer name before saving.', 'error');
  if (!currentItems.length) return setHint(statusEl, 'Add at least one item before saving.', 'error');
  if (computeTotals().discountTooBig) return setHint(statusEl, "Discount can't exceed the subtotal.", 'error');

  const payload = {
    customerName: name,
    items: currentItems,
    discountType: $('discountType').value,
    discountValue: parseFloat($('discountValue').value) || 0,
    paymentMethod: $('paymentMethod').value,
  };
  try {
    const wasEditing = !!editingBillId;
    const data = await api(wasEditing ? `/api/bills/${editingBillId}` : '/api/bills', {
      method: wasEditing ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    });
    editingBillId = data.id;
    editingBillNo = data.billNo;
    currentCashier = data.cashierName;
    if (!isAdmin()) viewOnly = true;   // cashiers can't edit a saved bill
    renderItems();
    setHint(statusEl, viewOnly
      ? `Bill ${data.billNo} saved. Print it, then press "New bill".`
      : wasEditing ? `Bill ${data.billNo} updated.` : `Bill ${data.billNo} saved.`, 'ok');
    await Promise.all([loadHistory(), loadProducts()]);   // stock has changed
    showStockHint();
  } catch (err) {
    setHint(statusEl, err.message || 'Could not save bill.', 'error');
  }
}
$('saveBillBtn').addEventListener('click', saveBill);

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------
async function loadHistory() {
  const listEl = $('historyList');
  const q = $('historySearch').value.trim();
  try {
    const bills = await api('/api/bills' + (q ? `?q=${encodeURIComponent(q)}` : ''));
    $('historyCount').textContent = bills.length;
    if (!bills.length) {
      listEl.innerHTML = `<div class="empty">${q ? 'No bills match your search.' : 'No saved bills yet.'}</div>`;
      return;
    }
    listEl.innerHTML = '';
    bills.forEach((bill) => {
      const div = document.createElement('div');
      div.className = 'history-item';
      const dateStr = new Date(bill.updatedAt + 'Z').toLocaleString();
      const by = isAdmin() && bill.cashierName ? ` · by ${escapeHtml(bill.cashierName)}` : '';
      div.innerHTML = `
        <div class="name">${escapeHtml(bill.customerName)} — ${money(bill.total)}</div>
        <div class="meta">${escapeHtml(bill.billNo)}<span class="badge">${escapeHtml(bill.paymentMethod)}</span> · ${bill.items.length} item(s) · ${dateStr}${by}</div>
        <div class="history-actions">
          <button data-action="load" data-id="${bill.id}">${isAdmin() ? 'Load' : 'View / reprint'}</button>
          ${isAdmin() ? `<button class="delete" data-action="delete" data-id="${bill.id}">Delete</button>` : ''}
        </div>`;
      listEl.appendChild(div);
    });
    listEl.querySelectorAll('button[data-action="load"]').forEach((b) =>
      b.addEventListener('click', () => loadBillIntoForm(b.dataset.id, bills))
    );
    listEl.querySelectorAll('button[data-action="delete"]').forEach((b) =>
      b.addEventListener('click', () => deleteBill(b.dataset.id))
    );
  } catch (err) {
    if (me) listEl.innerHTML = '<div class="empty">Could not load history.</div>';
  }
}

let searchTimer;
$('historySearch').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadHistory, 250);
});

function loadBillIntoForm(id, bills) {
  const bill = bills.find((b) => String(b.id) === String(id));
  if (!bill) return;
  $('customerName').value = bill.customerName;
  $('discountType').value = bill.discountType;
  $('discountValue').value = bill.discountValue || '';
  $('paymentMethod').value = bill.paymentMethod;
  currentBillDate = new Date(bill.createdAt + 'Z');
  currentCashier = bill.cashierName;
  currentItems = bill.items.map((it) => ({
    productId: it.productId, name: it.name, price: it.price, unit: it.unit,
    quantity: it.quantity, gstRate: it.gstRate || 0,
  }));
  editingBillId = bill.id;
  editingBillNo = bill.billNo;
  viewOnly = !isAdmin();
  matched = null;
  applyLock();
  showStockHint();
  renderItems();
  setHint($('saveStatus'), viewOnly ? `Viewing ${bill.billNo} — you can print it.` : `Editing ${bill.billNo} — save to update it (stock adjusts automatically).`);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function deleteBill(id) {
  if (!confirm('Delete this bill permanently? Its items go back into stock.')) return;
  try {
    await api(`/api/bills/${id}`, { method: 'DELETE' });
    if (String(editingBillId) === String(id)) resetBill();
    toast('Bill deleted, stock restored');
    await Promise.all([loadHistory(), loadProducts()]);
  } catch (err) {
    toast(err.message);
  }
}

// ---------------------------------------------------------------------------
// Products + stock (admin)
// ---------------------------------------------------------------------------
async function loadProducts() {
  products = await api('/api/products');
  $('productList').innerHTML = products.map((p) => {
    const bits = [p.sku, `₹${p.price}${p.unit ? '/' + p.unit : ''}`,
      p.stock === null ? '' : p.stock <= 0 ? 'out of stock' : `${qty(p.stock)} left`].filter(Boolean);
    return `<option value="${escapeHtml(p.name)}">${escapeHtml(bits.join(' · '))}</option>`;
  }).join('');
  if (matched) matched = products.find((p) => p.id === matched.id) || null;
  if (isAdmin()) renderProductTable();
}

function stockCell(p) {
  if (p.stock === null) return '<span class="sub">not tracked</span>';
  const pill = p.stock <= 0 ? '<span class="pill out">Out</span>' : p.lowStock ? '<span class="pill low">Low</span>' : '';
  return `${qty(p.stock)}${p.unit ? ' ' + escapeHtml(p.unit) : ''}${pill}`;
}

function renderProductTable() {
  const f = $('productSearch').value.trim().toLowerCase();
  const list = products.filter((p) => !f || p.name.toLowerCase().includes(f) || (p.sku || '').toLowerCase().includes(f));
  $('productCount').textContent = products.length;
  const el = $('productTable');
  if (!list.length) {
    el.innerHTML = `<div class="empty">${products.length ? 'No match.' : 'No products yet — add your first one.'}</div>`;
    return;
  }
  el.innerHTML = '';
  list.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'prow';
    row.innerHTML = `
      <div>${escapeHtml(p.name)}<div class="sub">${escapeHtml(p.sku || 'no barcode')}</div></div>
      <div>₹${p.price}${p.unit ? '/' + escapeHtml(p.unit) : ''}</div>
      <div>${p.gstRate}%</div>
      <div>${stockCell(p)}</div>
      <div class="btns"><button data-stock="${p.id}">Stock</button><button data-edit="${p.id}">Edit</button><button class="delete" data-del="${p.id}">Delete</button></div>`;
    el.appendChild(row);
  });
  el.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => editProduct(Number(b.dataset.edit))));
  el.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => removeProduct(Number(b.dataset.del))));
  el.querySelectorAll('[data-stock]').forEach((b) => b.addEventListener('click', () => openStockPanel(Number(b.dataset.stock))));
}

function resetProductForm() {
  editingProductId = null;
  ['pName', 'pSku', 'pPrice', 'pUnit', 'pReorder', 'pStock'].forEach((id) => ($(id).value = ''));
  $('pGst').value = '0';
  $('pStockField').classList.remove('hidden');
  $('productFormTitle').textContent = 'Add product';
  $('cancelProductBtn').classList.add('hidden');
}

function editProduct(id) {
  const p = products.find((x) => x.id === id);
  if (!p) return;
  editingProductId = id;
  $('pName').value = p.name; $('pSku').value = p.sku || ''; $('pPrice').value = p.price;
  $('pUnit').value = p.unit || ''; $('pGst').value = String(p.gstRate);
  $('pReorder').value = p.reorderLevel || '';
  $('pStockField').classList.add('hidden');   // stock only moves through the ledger
  $('productFormTitle').textContent = 'Edit product';
  $('cancelProductBtn').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function saveProduct() {
  const payload = {
    name: $('pName').value, sku: $('pSku').value, price: $('pPrice').value,
    unit: $('pUnit').value, gstRate: $('pGst').value, reorderLevel: $('pReorder').value,
    stock: $('pStock').value,
  };
  try {
    const editing = !!editingProductId;
    await api(editing ? `/api/products/${editingProductId}` : '/api/products', {
      method: editing ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    });
    setHint($('productStatus'), editing ? 'Product updated.' : 'Product added.', 'ok');
    resetProductForm();
    await loadProducts();
  } catch (err) {
    setHint($('productStatus'), err.message, 'error');
  }
}

async function removeProduct(id) {
  if (!confirm('Remove this product from the catalog? Saved bills are kept; its stock history is deleted.')) return;
  try {
    await api(`/api/products/${id}`, { method: 'DELETE' });
    if (stockProduct && stockProduct.id === id) closeStockPanel();
    await loadProducts();
  } catch (err) { toast(err.message); }
}

$('saveProductBtn').addEventListener('click', saveProduct);
$('cancelProductBtn').addEventListener('click', resetProductForm);
$('productSearch').addEventListener('input', renderProductTable);

// ----- stock panel -----
function describeMove(m) {
  const labels = { sale: 'Sale', opening: 'Opening stock', restock: 'Stock received', adjustment: 'Write-off / correction',
                   count: 'Stock count', bill_edit: 'Bill edited', bill_delete: 'Bill deleted (restored)' };
  return (labels[m.reason] || m.reason) + (m.billNo ? ` · ${m.billNo}` : '');
}

async function openStockPanel(id) {
  stockProduct = products.find((p) => p.id === id);
  if (!stockProduct) return;
  $('stockPanel').classList.remove('hidden');
  $('sQty').value = ''; $('sNote').value = ''; $('sMode').value = 'receive';
  setHint($('stockStatus'), '');
  await renderStockPanel();
  $('stockPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeStockPanel() {
  stockProduct = null;
  $('stockPanel').classList.add('hidden');
}

async function renderStockPanel() {
  const p = stockProduct;
  $('stockTitle').textContent = `Stock — ${p.name}`;
  setHint($('stockCurrent'), p.stock === null
    ? 'Not tracked yet. Use "Set exact count" to start tracking.'
    : `On hand: ${qty(p.stock)}${p.unit ? ' ' + p.unit : ''} · reorder at ${qty(p.reorderLevel)}`);
  try {
    const moves = await api(`/api/products/${p.id}/movements`);
    $('movements').innerHTML = moves.length ? moves.map((m) => `
      <div class="move">
        <span class="chg ${m.change >= 0 ? 'pos' : 'neg'}">${m.change >= 0 ? '+' : ''}${qty(m.change)}</span>
        <span>${escapeHtml(describeMove(m))}<div class="sub">${escapeHtml(m.username || '—')} · ${new Date(m.createdAt + 'Z').toLocaleString()}${m.note ? ' · ' + escapeHtml(m.note) : ''}</div></span>
        <span class="sub">= ${qty(m.balance)}</span>
      </div>`).join('') : '<div class="empty">No movements yet.</div>';
  } catch (err) { /* ignore */ }
}

$('applyStockBtn').addEventListener('click', async () => {
  const mode = $('sMode').value;
  const q = parseFloat($('sQty').value);
  if (isNaN(q) || q < 0 || (mode !== 'set' && q === 0)) {
    return setHint($('stockStatus'), 'Enter a valid quantity.', 'error');
  }
  const payload = mode === 'set' ? { set: q } : { change: mode === 'remove' ? -q : q };
  payload.note = $('sNote').value;
  try {
    const updated = await api(`/api/products/${stockProduct.id}/stock`, { method: 'POST', body: JSON.stringify(payload) });
    setHint($('stockStatus'), 'Stock updated.', 'ok');
    $('sQty').value = ''; $('sNote').value = '';
    await loadProducts();
    stockProduct = products.find((p) => p.id === updated.id) || updated;
    await renderStockPanel();
  } catch (err) {
    setHint($('stockStatus'), err.message, 'error');
  }
});
$('closeStockBtn').addEventListener('click', closeStockPanel);

// ---------------------------------------------------------------------------
// Dashboard (admin)
// ---------------------------------------------------------------------------
async function loadDashboard() {
  const s = await api(`/api/stats?tzOffset=${new Date().getTimezoneOffset()}`);
  $('statGrid').innerHTML = [
    ['Today', money(s.today.sales), `${s.today.bills} bill(s)`],
    ['This month', money(s.month.sales), `${s.month.bills} bill(s)`],
    ['All time', money(s.allTime.sales), `${s.allTime.bills} bill(s)`],
    ['Average bill', money(s.allTime.average), 'across all bills'],
  ].map(([l, v, sub]) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`).join('');

  const max = Math.max(1, ...s.last7.map((d) => d.sales));
  $('bars').innerHTML = s.last7.map((d, i) => `
    <div class="bar-col ${i === s.last7.length - 1 ? 'today' : ''}">
      <div class="bar-val">${d.sales ? '₹' + Math.round(d.sales) : ''}</div>
      <div class="bar" style="height:${(d.sales / max) * 78}%"></div>
      <div class="bar-lbl">${d.label}</div>
    </div>`).join('');

  const rank = (rows, left, right) => rows.length
    ? rows.map((r) => `<div class="rank"><span>${left(r)}</span><span>${right(r)}</span></div>`).join('')
    : '<div class="empty">Nothing yet.</div>';
  $('topItems').innerHTML = rank(s.topItems, (t) => escapeHtml(t.name), (t) => money(t.revenue));
  $('payMix').innerHTML = rank(s.payments, (p) => `${escapeHtml(p.method)} · ${p.bills}`, (p) => money(p.sales));
  $('cashierSales').innerHTML = rank(s.cashiers, (c) => `${escapeHtml(c.name)} · ${c.bills} bill(s)`, (c) => money(c.sales));
  $('lowStock').innerHTML = s.lowStock.length
    ? rank(s.lowStock, (l) => escapeHtml(l.name), (l) => `${qty(l.stock)}${l.unit ? ' ' + escapeHtml(l.unit) : ''} <small>(reorder ${qty(l.reorderLevel)})</small>`)
    : '<div class="empty">Everything tracked is above its reorder level.</div>';
}

// ---------------------------------------------------------------------------
// Team (admin)
// ---------------------------------------------------------------------------
async function loadUsers() {
  const users = await api('/api/users');
  $('userCount').textContent = users.length;
  const el = $('userTable');
  el.innerHTML = '';
  users.forEach((u) => {
    const mine = u.id === me.id;
    const row = document.createElement('div');
    row.className = 'prow userrow';
    row.innerHTML = `
      <div>${escapeHtml(u.username)}${mine ? ' <span class="sub">(you)</span>' : ''}${u.active ? '' : '<span class="pill off">disabled</span>'}
        <div class="sub">${u.bills} bill(s)</div></div>
      <div><span class="pill ${u.role}">${u.role}</span></div>
      <div></div>
      <div class="btns">${mine ? '' : `
        <button data-role="${u.id}" data-to="${u.role === 'admin' ? 'cashier' : 'admin'}">Make ${u.role === 'admin' ? 'cashier' : 'admin'}</button>
        <button data-reset="${u.id}" data-name="${escapeHtml(u.username)}">Reset password</button>
        <button class="delete" data-active="${u.id}" data-to="${u.active ? 0 : 1}">${u.active ? 'Disable' : 'Enable'}</button>`}</div>`;
    el.appendChild(row);
  });
  const update = async (id, body) => {
    try { await api(`/api/users/${id}`, { method: 'PUT', body: JSON.stringify(body) }); await loadUsers(); }
    catch (err) { toast(err.message); }
  };
  el.querySelectorAll('[data-role]').forEach((b) => b.addEventListener('click', () => update(b.dataset.role, { role: b.dataset.to })));
  el.querySelectorAll('[data-active]').forEach((b) => b.addEventListener('click', () => update(b.dataset.active, { active: b.dataset.to === '1' })));
  el.querySelectorAll('[data-reset]').forEach((b) => b.addEventListener('click', () => openPwDialog(Number(b.dataset.reset), b.dataset.name)));
}

$('addUserBtn').addEventListener('click', async () => {
  try {
    await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({ username: $('uName').value.trim(), password: $('uPass').value, role: $('uRole').value }),
    });
    $('uName').value = ''; $('uPass').value = '';
    setHint($('userStatus'), 'Account created. Share the temporary password privately.', 'ok');
    await loadUsers();
  } catch (err) {
    setHint($('userStatus'), err.message, 'error');
  }
});

// ---------------------------------------------------------------------------
// Tabs + boot
// ---------------------------------------------------------------------------
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
  $('view-' + name).classList.remove('hidden');
  const fail = (e) => toast(e.message);
  if (name === 'billing') { loadProducts().catch(fail); loadHistory(); }
  if (name === 'products') loadProducts().catch(fail);
  if (name === 'dashboard') loadDashboard().catch(fail);
  if (name === 'team') loadUsers().catch(fail);
}
document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

boot();
