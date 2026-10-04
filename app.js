"use strict";
/* واجهة نظام تسجيل الإيرادات اليومية - بدون مكتبات خارجية */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = n => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const r2 = n => Math.round((Number(n) + Number.EPSILON) * 100) / 100;
const STATUS = { done: "مكتملة", progress: "قيد الإنجاز", late: "متأخرة", pending: "لم تبدأ" };
const CLS = { surplus_normal: "فائض طبيعي", surplus_abnormal: "فائض غير طبيعي", shortage_normal: "عجز طبيعي", shortage_abnormal: "عجز غير طبيعي" };
const FIELD = { sys_cash: "نقدي (نظام)", sys_net: "شبكة (نظام)", act_cash: "نقدي (فعلي)", act_net: "شبكة (فعلي)", act_transfer: "تحويل بنكي", act_credit: "آجل", returns_count: "عدد فواتير المرتجع", returns_value: "قيمة المرتجع", notes: "ملاحظات", amount: "المبلغ", note: "ملاحظة", status: "الحالة", company: "المنشأة", emp_no: "الرقم الوظيفي", emp_name: "الاسم", emp_title: "المسمى", sys_rev: "إيراد النظام", act_rev: "الإيراد الفعلي", perms: "الصلاحيات", branch_ids: "الفروع", active: "فعّال" };
const ENTITY = { sales: "الإيرادات", terminal: "سحب الشبكات", variance: "العجز والفائض", task: "المهام", user: "المستخدمون", branch: "الفروع", terminal_def: "أجهزة الشبكة", settings: "الإعدادات", session: "الدخول", backup: "نسخ احتياطي" };
const ACTION = { create: "إضافة", update: "تعديل", delete: "حذف", login: "دخول", password: "تغيير كلمة المرور", backup: "نسخة احتياطية" };

const S = { user: null, meta: null, day: null, data: null, tab: "sales", date: null, month: null, timer: null, online: true, rows: {} };

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch("/api/" + path, o); } catch (e) { setOnline(false); throw { error: "لا يوجد اتصال بالخادم" }; }
  setOnline(true);
  if (res.status === 401 && path !== "login" && path !== "me") { S.user = null; render(); throw { error: "انتهت الجلسة" }; }
  if (opts.raw) return res;
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw { status: res.status, ...j };
  return j;
}
function setOnline(v) { S.online = v; const e = $("#live"); if (e) { e.classList.toggle("off", !v); e.lastChild.textContent = v ? "متصل - تحديث تلقائي" : "انقطع الاتصال"; } }
function toast(msg, kind = "") { const t = document.createElement("div"); t.className = "toast " + kind; t.textContent = msg; $("#toasts").append(t); setTimeout(() => t.remove(), kind ? 6000 : 3000); }
const can = p => S.user && (S.user.role === "admin" || S.user.perms.includes(p));
const today = () => S.meta?.today || new Date().toISOString().slice(0, 10);
function shiftDate(d, n) { const x = new Date(d + "T12:00:00"); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10); }
function editable() {
  if (S.user.role === "admin" || can("edit_past")) return true;
  const lim = shiftDate(today(), -S.meta.settings.past_days);
  return S.date >= lim && S.date <= today();
}

/* ------------------------------------------------------------ الهيكل العام */
function render() {
  clearInterval(S.timer);
  const app = $("#app");
  if (!S.user) return loginView(app);
  if (S.user.must_change) return changePwView(app, true);
  app.innerHTML = `
  <header class="top"><div><h1>مجموعة الرزيحي للكماليات - تسجيل الإيرادات اليومية</h1><div class="sub">${esc(S.user.full_name)} · ${S.user.role === "admin" ? "مدير الحسابات" : "محاسب"}</div></div><span class="sp"></span>
    <span class="live" id="live"><i></i><span>متصل - تحديث تلقائي</span></span>
    <button id="chpw">تغيير كلمة المرور</button><button id="lo">خروج</button></header>
  <div class="bar"><div class="tabs" id="tabs"></div>
    <div class="datebox" id="datebox"><button id="pd">›</button><input type="date" id="dt" value="${S.date}" max="${today()}"><button id="nd">‹</button><button id="td">اليوم</button>${can("export") ? `<a class="btn" id="ex" href="/api/export?date=${S.date}">تصدير CSV</a>` : ""}</div></div>
  <main id="main"></main>`;
  $("#lo").onclick = async () => { await api("logout", { method: "POST" }).catch(() => { }); S.user = null; render(); };
  $("#chpw").onclick = () => changePwView($("#app"), false);
  const tabs = [["sales", "الإيرادات اليومية"], ["terminals", "سحب الشبكات"], ["variance", "العجز والفائض غير الطبيعي"], ["checklist", "قائمة المتابعة اليومية"], ["history", "سجل التعديلات"]];
  if (S.user.role === "admin") tabs.push(["admin", "الإدارة"]);
  $("#tabs").innerHTML = tabs.map(([k, l]) => `<button data-t="${k}" class="${S.tab === k ? "on" : ""}">${l}</button>`).join("");
  $("#tabs").onclick = e => { const t = e.target.dataset.t; if (t) { S.tab = t; render(); } };
  const setDate = d => { if (d) { S.date = d > today() ? today() : d; render(); } };
  $("#dt").onchange = e => setDate(e.target.value);
  $("#pd").onclick = () => setDate(shiftDate(S.date, -1));
  $("#nd").onclick = () => setDate(shiftDate(S.date, 1));
  $("#td").onclick = () => setDate(today());
  if (["admin"].includes(S.tab)) $("#datebox").style.display = "none";
  load();
}

async function load() {
  const main = $("#main");
  try {
    if (S.tab === "admin") return adminView(main);
    if (S.tab === "history") return historyView(main);
    if (S.tab === "checklist") { S.timer = setInterval(() => checklistView(main, true), 15000); return checklistView(main); }
    S.data = await api("day?date=" + S.date);
    const view = { sales: salesView, terminals: terminalsView, variance: varianceView }[S.tab];
    view(main);
    S.timer = setInterval(poll, 8000);
  } catch (e) { if (e.error) main.innerHTML = `<div class="card err">${esc(e.error)}</div>`; }
}
async function poll() {
  if (document.hidden) return;
  try {
    const d = await api("day?date=" + S.date);
    if (JSON.stringify(d) === JSON.stringify(S.data) || d.day !== S.date) return;
    S.data = d;
    ({ sales: salesView, terminals: terminalsView, variance: varianceView })[S.tab]($("#main"), true);
  } catch (e) { }
}

/* ------------------------------------------------------------ تسجيل الدخول */
function loginView(app) {
  app.innerHTML = `<form class="login" id="lf"><h1>مجموعة الرزيحي للكماليات</h1><div class="muted" style="margin-bottom:16px">نظام تسجيل الإيرادات اليومية</div>
    <div class="err" id="le"></div><label>اسم المستخدم<input id="lu" autocomplete="username" required autofocus></label>
    <label>كلمة المرور<input id="lp" type="password" autocomplete="current-password" required></label><button class="btn pri">دخول</button></form>`;
  $("#lf").onsubmit = async e => {
    e.preventDefault();
    try { S.user = await api("login", { method: "POST", body: { username: $("#lu").value, password: $("#lp").value } }); await boot(); }
    catch (x) { $("#le").textContent = x.error || "تعذر الدخول"; }
  };
}
function changePwView(app, forced) {
  app.innerHTML = `<form class="login" id="cf"><h1>${forced ? "يجب تغيير كلمة المرور" : "تغيير كلمة المرور"}</h1><div class="err" id="ce"></div>
    <label>كلمة المرور الحالية<input id="co" type="password" required></label><label>كلمة المرور الجديدة (8 أحرف فأكثر)<input id="cn" type="password" minlength="8" required></label>
    <button class="btn pri">حفظ</button>${forced ? "" : '<button type="button" class="btn" id="cc" style="margin-top:8px">إلغاء</button>'}</form>`;
  if (!forced) $("#cc").onclick = render;
  $("#cf").onsubmit = async e => {
    e.preventDefault();
    try { await api("change-password", { method: "POST", body: { old: $("#co").value, new: $("#cn").value } }); S.user.must_change = false; toast("تم تغيير كلمة المرور"); await boot(); }
    catch (x) { $("#ce").textContent = x.error; }
  };
}
async function boot() {
  S.meta = null;
  if (!S.user.must_change) S.meta = await api("meta");
  S.date = S.date || today();
  render();
}

/* ------------------------------------------------------------ إيرادات المبيعات */
const SALES_IN = ["sys_cash", "sys_net", "act_cash", "act_net", "act_transfer", "act_credit"];
function calc(r) {
  const sysT = r2(r.sys_cash + r.sys_net), actT = r2(r.act_cash + r.act_net + r.act_transfer + r.act_credit);
  const lc = r2(r.act_cash - r.sys_cash), ln = r2(r.act_net + r.act_transfer + r.act_credit - r.sys_net), net = r2(lc + ln), t = S.meta.settings.threshold;
  const cls = net >= t ? "surplus_abnormal" : net >= 0 ? "surplus_normal" : net > -t ? "shortage_normal" : "shortage_abnormal";
  return { sysT, actT, lc, ln, net, cls };
}
const zeroRec = () => ({ sys_cash: 0, sys_net: 0, act_cash: 0, act_net: 0, act_transfer: 0, act_credit: 0, returns_count: 0, returns_value: 0, notes: "", version: 0 });
const sgn = n => `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${fmt(n)}</span>`;

function salesView(main, refresh) {
  const { branches, sales } = S.data, ed = editable() && can("sales");
  const rec = Object.fromEntries(sales.map(s => [s.branch_id, s]));
  if (refresh) {  // تحديث الصفوف غير المعدَّلة حالياً فقط
    branches.forEach(b => { const tr = $(`tr[data-b="${b.id}"]`); if (tr && !tr.contains(document.activeElement) && !tr.dataset.dirty) fillSalesRow(tr, rec[b.id]); });
    return updateSalesTotals();
  }
  let rows = "", last = "", i = 0;
  branches.forEach(b => {
    if (b.company !== last) { last = b.company; rows += `<tr class="grp"><td colspan="20">${esc(b.company)}</td></tr>`; }
    rows += `<tr data-b="${b.id}"><td>${++i}</td><td class="l">${esc(b.name)}</td>` +
      ["sys_cash", "sys_net"].map(f => `<td><input type="number" step="0.01" data-f="${f}" ${ed ? "" : "disabled"}></td>`).join("") + `<td class="calc" data-c="sysT"></td>` +
      ["act_cash", "act_net", "act_transfer", "act_credit"].map(f => `<td><input type="number" step="0.01" data-f="${f}" ${ed ? "" : "disabled"}></td>`).join("") + `<td class="calc" data-c="actT"></td>` +
      `<td class="calc" data-c="lc"></td><td class="calc" data-c="ln"></td><td class="calc" data-c="net"></td><td data-c="cls"></td>` +
      `<td><input type="number" class="sm" min="0" step="1" data-f="returns_count" ${ed ? "" : "disabled"}></td><td><input type="number" class="sm" step="0.01" data-f="returns_value" ${ed ? "" : "disabled"}></td>` +
      `<td><input type="text" class="note" data-f="notes" maxlength="500" ${ed ? "" : "disabled"}></td><td class="muted" data-c="by"></td></tr>`;
  });
  main.innerHTML = `${ed ? "" : '<div class="card warn" style="color:var(--warn)">هذا اليوم للعرض فقط - لا تملك صلاحية التعديل عليه.</div>'}
  <div class="kpis" id="kpis"></div>
  <div class="tw"><table id="st"><thead><tr><th rowspan="2">م</th><th rowspan="2" class="l">الفرع</th><th colspan="3">مبيعات نظام الأوائل</th><th colspan="5">المبيعات الفعلية</th><th rowspan="2">فرق النقدي</th><th rowspan="2">فرق الشبكة</th><th rowspan="2">صافي الفرق</th><th rowspan="2">التصنيف</th><th colspan="2">المرتجعات</th><th rowspan="2">ملاحظات</th><th rowspan="2">آخر تعديل</th></tr>
  <tr><th>نقدي</th><th>شبكة</th><th>الإجمالي</th><th>نقدي</th><th>شبكة</th><th>تحويل بنكي</th><th>آجل</th><th>الإجمالي</th><th>عدد الفواتير</th><th>القيمة</th></tr></thead>
  <tbody>${rows}</tbody><tfoot><tr id="tot"></tr></tfoot></table></div>
  <p class="muted">الحقول المظللة تُحسب تلقائياً. العجز/الفائض الطبيعي أقل من ${S.meta.settings.threshold} ريال، وما زاد عن ذلك يُعدّ غير طبيعي. يُحفظ كل صف تلقائياً عند الانتقال لحقل آخر.</p>`;
  branches.forEach(b => fillSalesRow($(`tr[data-b="${b.id}"]`), rec[b.id]));
  updateSalesTotals();
  if (ed) {
    const t = $("#st");
    t.addEventListener("input", e => { const tr = e.target.closest("tr[data-b]"); if (tr) { tr.dataset.dirty = 1; paintCalc(tr); updateSalesTotals(); } });
    t.addEventListener("change", e => { const tr = e.target.closest("tr[data-b]"); if (tr) saveSales(tr); });
    t.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") { const ins = [...t.querySelectorAll("tbody input:not(:disabled)")]; ins[ins.indexOf(e.target) + 1]?.focus(); } });
  }
}
function rowVals(tr) {
  const v = {}; tr.querySelectorAll("[data-f]").forEach(i => v[i.dataset.f] = i.type === "number" ? (parseFloat(i.value) || 0) : i.value);
  return v;
}
function fillSalesRow(tr, rec) {
  const r = rec || zeroRec();
  tr._rec = rec || null; tr._ver = r.version;
  tr.querySelectorAll("[data-f]").forEach(i => { const v = r[i.dataset.f]; i.value = rec ? (i.type === "number" && v === 0 ? "" : v) : ""; });
  paintCalc(tr);
  tr.querySelector('[data-c="by"]').textContent = rec ? `${rec.updated_by || ""} ${(rec.updated_at || "").slice(11, 16)}` : "—";
}
function paintCalc(tr) {
  const c = calc(rowVals(tr)), set = (k, h) => tr.querySelector(`[data-c="${k}"]`).innerHTML = h;
  set("sysT", fmt(c.sysT)); set("actT", fmt(c.actT)); set("lc", sgn(c.lc)); set("ln", sgn(c.ln)); set("net", sgn(c.net));
  set("cls", tr._rec || tr.dataset.dirty ? `<span class="badge b-${c.cls}">${CLS[c.cls]}</span>` : "");
}
function updateSalesTotals() {
  const T = { sysC: 0, sysN: 0, aC: 0, aN: 0, aT: 0, aD: 0, rc: 0, rv: 0, net: 0 };
  let entered = 0, n = 0;
  document.querySelectorAll("tr[data-b]").forEach(tr => {
    n++; const v = rowVals(tr), c = calc(v);
    if (tr._rec || tr.dataset.dirty) entered++;
    T.sysC += v.sys_cash; T.sysN += v.sys_net; T.aC += v.act_cash; T.aN += v.act_net; T.aT += v.act_transfer; T.aD += v.act_credit; T.rc += v.returns_count; T.rv += v.returns_value; T.net += c.net;
  });
  const sysT = T.sysC + T.sysN, actT = T.aC + T.aN + T.aT + T.aD;
  const tot = $("#tot"); if (!tot) return;
  tot.innerHTML = `<td colspan="2">الإجمالي</td><td>${fmt(T.sysC)}</td><td>${fmt(T.sysN)}</td><td>${fmt(sysT)}</td><td>${fmt(T.aC)}</td><td>${fmt(T.aN)}</td><td>${fmt(T.aT)}</td><td>${fmt(T.aD)}</td><td>${fmt(actT)}</td><td>${sgn(r2(T.aC - T.sysC))}</td><td>${sgn(r2(T.aN + T.aT + T.aD - T.sysN))}</td><td>${sgn(r2(T.net))}</td><td></td><td>${T.rc}</td><td>${fmt(T.rv)}</td><td colspan="2"></td>`;
  $("#kpis").innerHTML = `<div class="kpi"><b>${fmt(sysT)}</b><span>إجمالي مبيعات النظام</span></div><div class="kpi"><b>${fmt(actT)}</b><span>إجمالي المبيعات الفعلية</span></div><div class="kpi"><b>${sgn(r2(T.net))}</b><span>صافي العجز/الفائض</span></div><div class="kpi"><b>${entered} / ${n}</b><span>فروع تم تسجيلها</span></div>`;
}
const SAVING = new Set();
async function saveSales(tr) {
  const key = tr.dataset.b;
  if (SAVING.has(key)) { tr.dataset.again = 1; return; }
  SAVING.add(key); tr.classList.add("saving");
  const body = { day: S.date, branch_id: +key, ...rowVals(tr), version: tr._ver || 0 };
  try {
    const rec = await api("sales", { method: "PUT", body });
    delete tr.dataset.dirty; tr.classList.remove("saving"); tr.classList.add("saved");
    const again = tr.dataset.again; delete tr.dataset.again;
    if (again) { const cur = rowVals(tr); fillSalesRow(tr, rec); Object.entries(cur).forEach(([f, v]) => { const i = tr.querySelector(`[data-f="${f}"]`); i.value = i.type === "number" && v === 0 ? "" : v; }); tr._ver = rec.version; paintCalc(tr); SAVING.delete(key); await saveSales(tr); return; }
    fillSalesRow(tr, rec);
    S.data.sales = S.data.sales.filter(s => s.branch_id !== rec.branch_id).concat(rec);
    setTimeout(() => tr.classList.remove("saved"), 1000);
  } catch (e) {
    tr.classList.remove("saving");
    if (e.status === 409) { delete tr.dataset.dirty; fillSalesRow(tr, e.current); tr.classList.add("conflict"); setTimeout(() => tr.classList.remove("conflict"), 4000); toast(`تعارض: عدّل ${e.current?.updated_by || "مستخدم آخر"} هذا الفرع قبل حفظك. تم تحميل آخر نسخة - راجعها ثم أعد الإدخال.`, "warn"); }
    else toast(e.error || "فشل الحفظ", "bad");
  }
  SAVING.delete(key); updateSalesTotals();
}

/* ------------------------------------------------------------ سحب الشبكات */
function terminalsView(main, refresh) {
  const { branches, terminals, terminal_entries, sales } = S.data, ed = editable() && can("terminals");
  const ent = Object.fromEntries(terminal_entries.map(e => [e.terminal_id, e])), sal = Object.fromEntries(sales.map(s => [s.branch_id, s]));
  if (refresh) {
    terminals.forEach(t => { const tr = $(`tr[data-t="${t.id}"]`); if (tr && !tr.contains(document.activeElement)) fillTermRow(tr, ent[t.id]); });
    return termSubtotals(sal);
  }
  let rows = "", last = "", i = 0;
  branches.forEach(b => {
    const ts = terminals.filter(t => t.branch_id === b.id); if (!ts.length) return;
    if (b.company !== last) { last = b.company; rows += `<tr class="grp"><td colspan="6">${esc(b.company)}</td></tr>`; }
    ts.forEach(t => { rows += `<tr data-t="${t.id}" data-br="${b.id}"><td>${++i}</td><td class="l">${esc(b.name)}</td><td class="l" dir="ltr">${esc(t.no)}</td><td><input type="number" step="0.01" data-f="amount" ${ed ? "" : "disabled"}></td><td><input type="text" class="note" data-f="note" maxlength="300" ${ed ? "" : "disabled"}></td><td class="muted" data-c="by"></td></tr>`; });
    rows += `<tr class="sub" data-sub="${b.id}"><td colspan="3" class="l"><b>إجمالي ${esc(b.name)}</b></td><td data-c="sum"></td><td colspan="2" class="l" data-c="cmp"></td></tr>`;
  });
  main.innerHTML = `${ed ? "" : '<div class="card" style="color:var(--warn)">هذا اليوم للعرض فقط.</div>'}<div class="card noprint"><b>سحب الشبكات اليومي بالفروع</b> <span class="muted">— يُقارَن مجموع كل فرع تلقائياً مع «شبكة (فعلي)» في شاشة الإيرادات.</span></div>
  <div class="tw"><table id="tt"><thead><tr><th>م</th><th class="l">الفرع</th><th class="l">رقم الجهاز</th><th>القيمة</th><th>ملاحظات</th><th>آخر تعديل</th></tr></thead><tbody>${rows}</tbody><tfoot><tr><td colspan="3">الإجمالي</td><td id="tsum"></td><td colspan="2"></td></tr></tfoot></table></div>`;
  terminals.forEach(t => fillTermRow($(`tr[data-t="${t.id}"]`), ent[t.id]));
  termSubtotals(sal);
  if (ed) {
    const t = $("#tt");
    t.addEventListener("input", e => { const tr = e.target.closest("tr[data-t]"); if (tr) { tr.dataset.dirty = 1; termSubtotals(sal); } });
    t.addEventListener("change", e => { const tr = e.target.closest("tr[data-t]"); if (tr) saveTerm(tr, sal); });
    t.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") { const ins = [...t.querySelectorAll("tbody input:not(:disabled)")]; ins[ins.indexOf(e.target) + 1]?.focus(); } });
  }
}
function fillTermRow(tr, rec) {
  tr._rec = rec || null; tr._ver = rec?.version || 0;
  tr.querySelector('[data-f="amount"]').value = rec ? (rec.amount === 0 ? "0" : rec.amount) : "";
  tr.querySelector('[data-f="note"]').value = rec?.note || "";
  tr.querySelector('[data-c="by"]').textContent = rec ? `${rec.updated_by || ""} ${(rec.updated_at || "").slice(11, 16)}` : "—";
}
function termSubtotals(sal) {
  let all = 0; const sums = {};
  document.querySelectorAll("tr[data-t]").forEach(tr => { const v = parseFloat(tr.querySelector('[data-f="amount"]').value) || 0; sums[tr.dataset.br] = r2((sums[tr.dataset.br] || 0) + v); all += v; });
  document.querySelectorAll("tr[data-sub]").forEach(tr => {
    const b = tr.dataset.sub, s = sums[b] || 0, rec = sal[b];
    tr.querySelector('[data-c="sum"]').innerHTML = `<b>${fmt(s)}</b>`;
    tr.querySelector('[data-c="cmp"]').innerHTML = !rec ? '<span class="muted">لم تُسجَّل مبيعات الفرع</span>' : Math.abs(s - rec.act_net) < 0.005 ? `<span class="badge b-ok">مطابق لشبكة الإيرادات (${fmt(rec.act_net)})</span>` : `<span class="badge b-bad">غير مطابق: شبكة الإيرادات ${fmt(rec.act_net)} · الفرق ${fmt(r2(s - rec.act_net))}</span>`;
  });
  $("#tsum").textContent = fmt(all);
}
async function saveTerm(tr, sal) {
  const key = tr.dataset.t;
  if (SAVING.has("t" + key)) return; SAVING.add("t" + key); tr.classList.add("saving");
  try {
    const rec = await api("terminal", { method: "PUT", body: { day: S.date, terminal_id: +key, amount: tr.querySelector('[data-f="amount"]').value, note: tr.querySelector('[data-f="note"]').value, version: tr._ver } });
    delete tr.dataset.dirty; fillTermRow(tr, rec); tr.classList.add("saved"); setTimeout(() => tr.classList.remove("saved"), 1000);
  } catch (e) {
    if (e.status === 409) { delete tr.dataset.dirty; fillTermRow(tr, e.current); toast(`تعارض: عدّل ${e.current?.updated_by || "مستخدم آخر"} هذا الجهاز قبل حفظك. تم تحميل آخر نسخة.`, "warn"); }
    else toast(e.error || "فشل الحفظ", "bad");
  }
  tr.classList.remove("saving"); SAVING.delete("t" + key); termSubtotals(sal);
}

/* ------------------------------------------------------------ العجز والفائض غير الطبيعي */
function varianceView(main, refresh) {
  const { branches, variance } = S.data, ed = editable() && can("variance");
  if (refresh && $("#vf")?.dataset.editing) return;
  const rows = variance.map(v => { const d = r2(v.act_rev - v.sys_rev); return `<tr><td class="l">${esc(v.company)}</td><td class="l">${esc(v.branch)}</td><td>${esc(v.emp_no)}</td><td class="l">${esc(v.emp_name)}</td><td class="l">${esc(v.emp_title)}</td><td>${fmt(v.sys_rev)}</td><td>${fmt(v.act_rev)}</td><td>${sgn(d)}</td><td><span class="badge ${d > 0 ? "b-surplus_abnormal" : "b-shortage_abnormal"}">${d > 0 ? "فائض غير طبيعي" : d < 0 ? "عجز غير طبيعي" : "—"}</span></td><td class="l" style="white-space:normal">${esc(v.notes)}</td><td class="muted">${esc(v.updated_by)}</td>${ed ? `<td><button class="btn" data-e="${v.id}">تعديل</button> <button class="btn dng" data-d="${v.id}">حذف</button></td>` : ""}</tr>`; }).join("");
  main.innerHTML = `${ed ? `<form class="card form" id="vf"><h2 style="width:100%">تسجيل حالة عجز/فائض غير طبيعي</h2>
    <label>المنشأة<input name="company" required maxlength="100"></label>
    <label>الفرع<select name="branch_id" required>${branches.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select></label>
    <label>الرقم الوظيفي<input name="emp_no" maxlength="30" style="min-width:90px"></label><label>الاسم<input name="emp_name" required maxlength="100"></label><label>المسمى الوظيفي<input name="emp_title" maxlength="100"></label>
    <label>إيراد النظام<input type="number" step="0.01" name="sys_rev" required style="min-width:100px"></label><label>الإيراد الفعلي<input type="number" step="0.01" name="act_rev" required style="min-width:100px"></label>
    <label style="flex:1;min-width:200px">ملاحظات<input name="notes" maxlength="1000" style="width:100%"></label><button class="btn pri" id="vs">إضافة</button><button type="button" class="btn" id="vc" hidden>إلغاء التعديل</button></form>` : ""}
  <div class="tw"><table><thead><tr><th class="l">المنشأة</th><th class="l">الفرع</th><th>الرقم الوظيفي</th><th class="l">الاسم</th><th class="l">المسمى</th><th>إيراد النظام</th><th>الإيراد الفعلي</th><th>الفرق</th><th>الحركة</th><th class="l">ملاحظات</th><th>بواسطة</th>${ed ? "<th></th>" : ""}</tr></thead>
  <tbody>${rows || `<tr><td colspan="12" class="muted">لا توجد حالات مسجلة لهذا اليوم</td></tr>`}</tbody></table></div>`;
  if (!ed) return;
  const f = $("#vf"); let edit = null;
  f.onsubmit = async e => {
    e.preventDefault(); const b = Object.fromEntries(new FormData(f)); b.day = S.date; b.branch_id = +b.branch_id;
    try {
      if (edit) { b.version = edit.version; await api("variance/" + edit.id, { method: "PUT", body: b }); } else await api("variance", { method: "POST", body: b });
      toast("تم الحفظ"); S.data = await api("day?date=" + S.date); varianceView(main);
    } catch (x) { if (x.status === 409) { toast(x.error, "warn"); S.data = await api("day?date=" + S.date); varianceView(main); } else toast(x.error, "bad"); }
  };
  main.onclick = async e => {
    const id = e.target.dataset.e || e.target.dataset.d; if (!id) return;
    const v = variance.find(x => x.id == id);
    if (e.target.dataset.d) { if (!confirm("حذف هذا السجل؟ (يبقى في سجل التعديلات)")) return; try { await api("variance/" + id, { method: "DELETE" }); S.data = await api("day?date=" + S.date); varianceView(main); } catch (x) { toast(x.error, "bad"); } return; }
    edit = v; f.dataset.editing = 1; ["company", "branch_id", "emp_no", "emp_name", "emp_title", "sys_rev", "act_rev", "notes"].forEach(k => f.elements[k].value = v[k]);
    $("#vs").textContent = "حفظ التعديل"; $("#vc").hidden = false; f.scrollIntoView();
  };
  $("#vc").onclick = () => { edit = null; varianceView(main); };
}

/* ------------------------------------------------------------ قائمة المتابعة (لوحة مدير الحسابات) */
async function checklistView(main, quiet) {
  if (quiet && (document.activeElement?.closest?.("#detail") || document.hidden)) return;
  S.month = S.month || S.date.slice(0, 7);
  const [cl, day] = await Promise.all([api("checklist?month=" + S.month), api("day?date=" + S.date)]);
  S.data = day;
  const stCell = c => `<td><span class="badge b-${c.status}">${STATUS[c.status]}</span> <span class="muted">${c.done}/${c.total}</span>${c.notes ? ' <span title="توجد ملاحظات">💬</span>' : ""}</td>`;
  const todayRow = cl.days.find(d => d.day === today());
  const sum = k => cl.days.filter(d => d.tasks[k].status === "late").length;
  main.innerHTML = `<div class="card form noprint"><label>الشهر<input type="month" id="cm" value="${S.month}" max="${today().slice(0, 7)}"></label>
    <span class="muted">يظهر كل يوم بحالة كل مهمة على مستوى جميع الفروع (${cl.branches} فرع). اضغط على اليوم لعرض تفاصيل الفروع وتحديث الحالات وإضافة الملاحظات.</span></div>
  <div class="kpis">${cl.tasks.map(t => `<div class="kpi"><b class="${sum(t.key) ? "neg" : "pos"}">${sum(t.key)}</b><span>أيام متأخرة: ${esc(t.label)}</span></div>`).join("")}</div>
  <div class="tw" style="max-height:340px"><table><thead><tr><th>اليوم</th>${cl.tasks.map(t => `<th>${esc(t.label)}</th>`).join("")}<th>فروع سُجّلت مبيعاتها</th></tr></thead><tbody>
  ${cl.days.map(d => `<tr class="${d.day === S.date ? "sel" : ""}" data-d="${d.day}" style="cursor:pointer"><td><b>${d.day}</b> <span class="muted">${new Date(d.day + "T12:00").toLocaleDateString("ar-SA", { weekday: "long" })}</span></td>${cl.tasks.map(t => stCell(d.tasks[t.key])).join("")}<td>${d.sales_rows} / ${cl.branches}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">لا أيام في هذا الشهر</td></tr>'}</tbody></table></div>
  <div class="sp8"></div><div id="detail"></div>`;
  $("#cm").onchange = e => { S.month = e.target.value; checklistView(main); };
  main.querySelector("tbody").onclick = e => { const tr = e.target.closest("tr[data-d]"); if (tr) { S.date = tr.dataset.d; $("#dt").value = S.date; checklistView(main); } };
  detailView(cl);
}
function effStatus(st, branchTasks) { return st === "done" || st === "progress" ? st : (S.date < today() ? "late" : st); }
function detailView(cl) {
  const { branches, tasks } = S.data, ed = editable() && can("tasks");
  const tmap = Object.fromEntries(tasks.map(t => [t.branch_id + ":" + t.task, t])), sal = Object.fromEntries(S.data.sales.map(s => [s.branch_id, s]));
  $("#detail").innerHTML = `<div class="card"><h2>تفاصيل يوم ${S.date}</h2><div class="tw" style="max-height:none"><table id="dt2"><thead><tr><th class="l">الفرع</th>${cl.tasks.map(t => `<th>${esc(t.label)}</th>`).join("")}<th>مطابقة الشبكات</th></tr></thead><tbody>
  ${branches.map(b => `<tr><td class="l"><b>${esc(b.name)}</b></td>${cl.tasks.map(t => { const r = tmap[b.id + ":" + t.key], st = r?.status || "pending"; return `<td data-b="${b.id}" data-k="${t.key}"><select ${ed ? "" : "disabled"}>${Object.entries(STATUS).map(([k, l]) => `<option value="${k}" ${k === st ? "selected" : ""}>${l}</option>`).join("")}</select><br><input type="text" placeholder="ملاحظة المحاسب" value="${esc(r?.note || "")}" maxlength="500" ${ed ? "" : "disabled"} style="width:150px;margin-top:3px"><div class="muted">${r ? esc(r.updated_by) + " " + r.updated_at.slice(11, 16) : ""}</div></td>`; }).join("")}${(() => { const s = sal[b.id], tsum = S.data.terminal_entries.filter(e => S.data.terminals.find(t => t.id === e.terminal_id)?.branch_id === b.id).reduce((a, e) => a + e.amount, 0); return !s ? '<td><span class="badge b-late">لا مبيعات</span></td>' : Math.abs(tsum - s.act_net) < .005 ? '<td><span class="badge b-ok">مطابقة</span></td>' : `<td><span class="badge b-bad">فرق ${fmt(r2(tsum - s.act_net))}</span></td>`; })()}</tr>`).join("")}</tbody></table></div></div>`;
  if (!ed) return;
  $("#dt2").onchange = async e => {
    const td = e.target.closest("td[data-k]"); if (!td) return; const r = tmap[td.dataset.b + ":" + td.dataset.k];
    try {
      await api("task", { method: "PUT", body: { day: S.date, branch_id: +td.dataset.b, task: td.dataset.k, status: td.querySelector("select").value, note: td.querySelector("input").value, version: r?.version || 0 } });
      toast("تم الحفظ"); checklistView($("#main"));
    } catch (x) { toast(x.error, x.status === 409 ? "warn" : "bad"); if (x.status === 409) checklistView($("#main")); }
  };
}

/* ------------------------------------------------------------ سجل التعديلات */
async function historyView(main) {
  main.innerHTML = `<div class="card form noprint"><label>اليوم<input type="date" id="hd" value="${S.date}"></label><label>النوع<select id="he"><option value="">الكل</option>${Object.entries(ENTITY).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label><button class="btn pri" id="hg">عرض</button> <button class="btn" id="hall">كل الأيام</button></div><div id="hl"></div>`;
  const run = async all => {
    const qs = new URLSearchParams(); if (!all) qs.set("date", $("#hd").value); if ($("#he").value) qs.set("entity", $("#he").value);
    const rows = await api("history?" + qs);
    const diff = r => { let o = r.old ? JSON.parse(r.old) : null, n = r.new ? JSON.parse(r.new) : null; if (!n && !o) return ""; if (!o) return Object.entries(n).filter(([, v]) => v !== "" && v !== 0 && v !== null).map(([k, v]) => `${FIELD[k] || k}: <ins>${esc(Array.isArray(v) ? v.join("،") : v)}</ins>`).join(" · "); if (!n) return Object.entries(o).map(([k, v]) => `${FIELD[k] || k}: <s>${esc(v)}</s>`).join(" · "); return Object.keys(n).filter(k => JSON.stringify(o[k]) !== JSON.stringify(n[k])).map(k => `${FIELD[k] || k}: <s>${esc(Array.isArray(o[k]) ? o[k].join("،") : o[k])}</s> ← <ins>${esc(Array.isArray(n[k]) ? n[k].join("،") : n[k])}</ins>`).join(" · "); };
    $("#hl").innerHTML = `<div class="tw"><table><thead><tr><th>الوقت</th><th>المستخدم</th><th>العملية</th><th>النوع</th><th>اليوم</th><th>المرجع</th><th class="l">التفاصيل</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.ts)}</td><td>${esc(r.username)}</td><td>${ACTION[r.action] || r.action}</td><td>${ENTITY[r.entity] || r.entity}</td><td>${esc(r.day || "")}</td><td>${esc(r.ref || "")}</td><td class="diff">${diff(r)}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">لا يوجد سجل</td></tr>'}</tbody></table></div>`;
  };
  $("#hg").onclick = () => run(false); $("#hall").onclick = () => run(true); run(false);
}

/* ------------------------------------------------------------ الإدارة */
async function adminView(main) {
  const [users, bk, meta] = await Promise.all([api("users"), api("backups"), api("meta")]);
  S.meta = meta; const brs = meta.all_branches, trs = meta.all_terminals;
  main.innerHTML = `
  <div class="card"><h2>المستخدمون والصلاحيات</h2><div class="tw" style="max-height:none"><table><thead><tr><th class="l">المستخدم</th><th class="l">الاسم</th><th>الدور</th><th class="l">الصلاحيات</th><th class="l">الفروع</th><th>الحالة</th><th></th></tr></thead><tbody>
  ${users.map(u => `<tr><td class="l" dir="ltr">${esc(u.username)}</td><td class="l">${esc(u.full_name)}</td><td>${u.role === "admin" ? "مدير" : "محاسب"}</td><td class="l" style="white-space:normal">${u.role === "admin" ? "كل الصلاحيات" : u.perms.map(p => esc(meta.perms[p])).join("، ") || "—"}</td><td class="l" style="white-space:normal">${u.role === "admin" || !u.branch_ids.length ? "كل الفروع" : u.branch_ids.map(i => esc(brs.find(b => b.id === i)?.name)).join("، ")}</td><td><span class="badge ${u.active ? "b-ok" : "b-bad"}">${u.active ? "فعّال" : "موقوف"}</span></td><td><button class="btn" data-eu="${u.id}">تعديل</button></td></tr>`).join("")}</tbody></table></div>
  <div class="sp8"></div><button class="btn pri" id="nu">+ إضافة محاسب</button><div id="uf"></div></div>
  <div class="card"><h2>الإعدادات</h2><div class="form"><label>حد العجز/الفائض الطبيعي (ريال)<input type="number" id="s1" value="${meta.settings.threshold}" min="0" step="0.5"></label>
  <label>عدد الأيام السابقة المسموح للمحاسب بتعديلها<input type="number" id="s2" value="${meta.settings.past_days}" min="0"></label>
  <label>ساعة اعتبار مهام اليوم الحالي متأخرة (0-23)<input type="number" id="s3" value="${meta.settings.deadline_hour}" min="0" max="23"></label><button class="btn pri" id="ss">حفظ الإعدادات</button></div></div>
  <div class="card"><h2>الفروع وأجهزة الشبكة</h2><div class="form"><label>المنشأة<input id="bc" list="cos"></label><datalist id="cos">${[...new Set(brs.map(b => b.company))].map(c => `<option value="${esc(c)}">`).join("")}</datalist><label>اسم الفرع<input id="bn"></label><button class="btn" id="ba">إضافة فرع</button>
  <label>فرع الجهاز<select id="tb">${brs.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select></label><label>رقم الجهاز<input id="tn" dir="ltr"></label><button class="btn" id="ta">إضافة جهاز</button></div>
  <div class="sp8"></div><div class="cols">${brs.map(b => `<div><label class="chk"><input type="checkbox" data-bact="${b.id}" ${b.active ? "checked" : ""}> <b>${esc(b.name)}</b></label>${trs.filter(t => t.branch_id === b.id).map(t => `<label class="chk" style="padding-inline-start:22px"><input type="checkbox" data-tact="${t.id}" ${t.active ? "checked" : ""}><span dir="ltr" class="muted">${esc(t.no)}</span></label>`).join("")}</div>`).join("")}</div>
  <p class="muted">إلغاء التفعيل يخفي الفرع/الجهاز من الإدخال دون حذف بياناته التاريخية.</p></div>
  <div class="card"><h2>النسخ الاحتياطي</h2><p class="muted">يُنشأ نسخ تلقائي كل 6 ساعات (ويحتفظ بآخر 60 نسخة). يمكنك إنشاء نسخة فورية وتنزيلها.</p><button class="btn pri" id="mb">إنشاء نسخة احتياطية الآن</button><div class="sp8"></div>
  <div class="tw" style="max-height:260px"><table><thead><tr><th class="l">الملف</th><th>التاريخ</th><th>الحجم</th><th></th></tr></thead><tbody>${bk.map(b => `<tr><td class="l" dir="ltr">${esc(b.name)}</td><td>${b.time}</td><td>${(b.size / 1024).toFixed(0)} ك.ب</td><td><a class="btn" href="/api/backups/${encodeURIComponent(b.name)}">تنزيل</a></td></tr>`).join("") || '<tr><td colspan="4" class="muted">لا نسخ بعد</td></tr>'}</tbody></table></div></div>`;
  const reload = () => adminView(main);
  $("#ss").onclick = async () => { try { await api("settings", { method: "PUT", body: { threshold: $("#s1").value, past_days: $("#s2").value, deadline_hour: $("#s3").value } }); toast("تم الحفظ"); reload(); } catch (e) { toast(e.error, "bad"); } };
  $("#mb").onclick = async () => { try { const r = await api("backups", { method: "POST" }); toast("تم إنشاء " + r.name); reload(); } catch (e) { toast(e.error, "bad"); } };
  $("#ba").onclick = async () => { try { await api("branches", { method: "POST", body: { company: $("#bc").value, name: $("#bn").value } }); reload(); } catch (e) { toast(e.error, "bad"); } };
  $("#ta").onclick = async () => { try { await api("terminals", { method: "POST", body: { action: "add", branch_id: +$("#tb").value, no: $("#tn").value } }); reload(); } catch (e) { toast(e.error, "bad"); } };
  main.onchange = async e => {
    try {
      if (e.target.dataset.bact) await api("branches", { method: "PUT", body: { id: +e.target.dataset.bact, active: e.target.checked } });
      if (e.target.dataset.tact) await api("terminals", { method: "PUT", body: { id: +e.target.dataset.tact, active: e.target.checked } });
    } catch (x) { toast(x.error, "bad"); }
  };
  const userForm = u => {
    const isNew = !u; u = u || { username: "", full_name: "", perms: ["sales", "terminals", "variance", "tasks"], branch_ids: [], active: true, role: "accountant" };
    $("#uf").innerHTML = `<form class="card" id="uform" style="margin-top:12px"><h2>${isNew ? "إضافة محاسب جديد" : "تعديل " + esc(u.full_name)}</h2><div class="form">
      <label>اسم المستخدم (إنجليزي)<input name="username" ${isNew ? "required" : "disabled"} value="${esc(u.username)}" dir="ltr" pattern="[a-zA-Z0-9._-]+"></label><label>الاسم الكامل<input name="full_name" required value="${esc(u.full_name)}"></label>
      <label>${isNew ? "كلمة مرور مؤقتة (8+)" : "كلمة مرور جديدة (اختياري)"}<input name="password" type="text" minlength="8" ${isNew ? "required" : ""} dir="ltr" autocomplete="off"></label>
      ${isNew || u.role === "admin" ? "" : `<label class="chk"><input type="checkbox" name="active" ${u.active ? "checked" : ""}> الحساب فعّال</label>`}</div>
      ${u.role === "admin" ? '<p class="muted">حساب المدير يملك كل الصلاحيات.</p>' : `<h2 style="margin-top:12px">الصلاحيات</h2><div class="cols">${Object.entries(meta.perms).map(([k, l]) => `<label class="chk"><input type="checkbox" name="perm" value="${k}" ${u.perms.includes(k) ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div>
      <h2 style="margin-top:12px">الفروع المسموحة <span class="muted">(بدون تحديد = كل الفروع)</span></h2><div class="cols">${brs.map(b => `<label class="chk"><input type="checkbox" name="br" value="${b.id}" ${u.branch_ids.includes(b.id) ? "checked" : ""}> ${esc(b.name)}</label>`).join("")}</div>`}
      <div class="sp8"></div><button class="btn pri">حفظ</button> <button type="button" class="btn" id="ucn">إلغاء</button></form>`;
    $("#ucn").onclick = () => $("#uf").innerHTML = "";
    $("#uform").onsubmit = async e => {
      e.preventDefault(); const f = e.target;
      const body = { full_name: f.full_name.value, password: f.password.value || undefined, perms: [...f.querySelectorAll('[name=perm]:checked')].map(i => i.value), branch_ids: [...f.querySelectorAll('[name=br]:checked')].map(i => +i.value) };
      if (f.active) body.active = f.active.checked;
      try {
        if (isNew) { body.username = f.username.value; await api("users", { method: "POST", body }); } else await api("users/" + u.id, { method: "PUT", body });
        toast("تم الحفظ"); reload();
      } catch (x) { toast(x.error, "bad"); }
    };
    $("#uf").scrollIntoView({ behavior: "smooth" });
  };
  $("#nu").onclick = () => userForm();
  main.querySelectorAll("[data-eu]").forEach(b => b.onclick = () => userForm(users.find(u => u.id == b.dataset.eu)));
}

/* ------------------------------------------------------------ بدء التشغيل */
(async () => {
  try { S.user = await api("me"); await boot(); } catch (e) { render(); }
})();
