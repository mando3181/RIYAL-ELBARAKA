#!/usr/bin/env python3
"""نظام تسجيل الإيرادات اليومية - مجموعة الرزيحي للكماليات.

خادم ويب بمكتبة بايثون القياسية فقط (بدون أي تثبيت إضافي):
  - SQLite (وضع WAL) لقاعدة البيانات
  - جلسات بكوكي HttpOnly + كلمات مرور PBKDF2
  - تحكم بالتزامن عبر أرقام الإصدار (Optimistic Locking)
  - سجل تدقيق كامل لكل تعديل + نسخ احتياطي تلقائي

التشغيل:  python server.py            (المنفذ 8080)
          python server.py --port 9000 --host 0.0.0.0 --seed-sample
"""
import argparse, csv, hashlib, hmac, io, json, mimetypes, os, secrets, sqlite3, sys, threading, time
from datetime import date, datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

if hasattr(time, "tzset"):  # على لينكس: التوقيت المحلي (القاهرة) بدل UTC
    os.environ.setdefault("TZ", "Africa/Cairo")
    time.tzset()

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
DB_PATH = os.path.join(DATA_DIR, "revenue.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
STATIC = os.path.join(BASE, "static")
SESSION_HOURS = 12

TASKS = [
    ("receive", "استقبال التقارير من الفروع"),
    ("register", "تسجيل البيانات على النظام"),
    ("compare", "مقارنة البيانات المسجلة مع التقارير الأصلية"),
    ("verify", "التحقق من صحة البيانات"),
    ("send", "إرسال التقارير النهائية"),
]
TASK_KEYS = [t[0] for t in TASKS]
STATUSES = ("done", "progress", "late", "pending")
# صلاحيات المحاسب (يمنحها ويسحبها مدير الحسابات)
PERMS = {
    "sales": "تسجيل إيرادات المبيعات",
    "terminals": "تسجيل سحب الشبكات",
    "variance": "تسجيل ملاحظات العجز والفائض",
    "tasks": "تحديث حالة المهام اليومية",
    "edit_past": "تعديل أيام سابقة (بلا حد زمني)",
    "export": "تصدير التقارير",
}
SALES_FIELDS = ["sys_cash", "sys_net", "act_cash", "act_net", "act_transfer", "act_credit", "returns_count", "returns_value"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'accountant',
  perms TEXT NOT NULL DEFAULT '[]', branch_ids TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS branches(
  id INTEGER PRIMARY KEY, company TEXT NOT NULL, name TEXT NOT NULL UNIQUE, sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS terminals(
  id INTEGER PRIMARY KEY, branch_id INTEGER NOT NULL, no TEXT NOT NULL UNIQUE, sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS daily_sales(
  day TEXT NOT NULL, branch_id INTEGER NOT NULL,
  sys_cash REAL NOT NULL DEFAULT 0, sys_net REAL NOT NULL DEFAULT 0, act_cash REAL NOT NULL DEFAULT 0,
  act_net REAL NOT NULL DEFAULT 0, act_transfer REAL NOT NULL DEFAULT 0, act_credit REAL NOT NULL DEFAULT 0,
  returns_count INTEGER NOT NULL DEFAULT 0, returns_value REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, updated_by TEXT, updated_at TEXT, PRIMARY KEY(day, branch_id));
CREATE TABLE IF NOT EXISTS terminal_entries(
  day TEXT NOT NULL, terminal_id INTEGER NOT NULL, amount REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, updated_by TEXT, updated_at TEXT, PRIMARY KEY(day, terminal_id));
CREATE TABLE IF NOT EXISTS variance_notes(
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', branch_id INTEGER NOT NULL,
  emp_no TEXT NOT NULL DEFAULT '', emp_name TEXT NOT NULL DEFAULT '', emp_title TEXT NOT NULL DEFAULT '',
  sys_rev REAL NOT NULL DEFAULT 0, act_rev REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, updated_by TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS task_status(
  day TEXT NOT NULL, branch_id INTEGER NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  note TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, updated_by TEXT, updated_at TEXT,
  PRIMARY KEY(day, branch_id, task));
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, entity TEXT NOT NULL,
  day TEXT, ref TEXT, old TEXT, new TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_day ON audit_log(day);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DB_LOCK = threading.RLock()  # كاتب واحد في كل لحظة؛ مع نسخ الإصدار يمنع التضارب


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


DB = None


def q(sql, args=()):
    with DB_LOCK:
        return [dict(r) for r in DB.execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def get_setting(key, default):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key, value):
    with DB_LOCK:
        DB.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        DB.commit()


def audit(user, action, entity, day=None, ref=None, old=None, new=None):
    DB.execute("INSERT INTO audit_log(ts,username,action,entity,day,ref,old,new) VALUES(?,?,?,?,?,?,?,?)",
               (now(), user, action, entity, day, str(ref) if ref is not None else None,
                json.dumps(old, ensure_ascii=False) if old is not None else None,
                json.dumps(new, ensure_ascii=False) if new is not None else None))


def create_user(username, full_name, password, role="accountant", perms=None, branch_ids=None, must_change=1):
    salt = secrets.token_hex(16)
    with DB_LOCK:
        DB.execute("INSERT INTO users(username,full_name,pw_hash,salt,role,perms,branch_ids,must_change,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (username, full_name, hash_pw(password, salt), salt, role, json.dumps(perms or []), json.dumps(branch_ids or []), must_change, now()))
        DB.commit()


def init_db(seed_sample=False):
    global DB
    os.makedirs(BACKUP_DIR, exist_ok=True)
    DB = connect()
    with DB_LOCK:
        DB.executescript(SCHEMA)
        DB.commit()
    seed = json.load(open(os.path.join(BASE, "seed.json"), encoding="utf-8"))
    if not q1("SELECT 1 FROM branches"):
        with DB_LOCK:
            for i, b in enumerate(seed["branches"], 1):
                DB.execute("INSERT INTO branches(company,name,sort) VALUES(?,?,?)", (b["company"], b["name"], i))
            bid = {r["name"]: r["id"] for r in q("SELECT id,name FROM branches")}
            for i, t in enumerate(seed["terminals"], 1):
                DB.execute("INSERT INTO terminals(branch_id,no,sort) VALUES(?,?,?)", (bid[t["branch"]], t["no"], i))
            DB.commit()
        set_setting("threshold", 7)
        set_setting("past_days", 2)
        set_setting("deadline_hour", 18)
    if not q1("SELECT 1 FROM users WHERE role='admin'"):
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        create_user("admin", "مدير الحسابات", pw, "admin", list(PERMS), [], 1)
        print("=" * 60)
        print("  تم إنشاء حساب مدير الحسابات:  admin")
        print("  كلمة المرور المؤقتة:", pw)
        print("  (سيُطلب تغييرها عند أول دخول)")
        print("=" * 60)
    if seed_sample and not q1("SELECT 1 FROM daily_sales"):
        d = seed["sample_date"]
        bid = {r["name"]: r["id"] for r in q("SELECT id,name FROM branches")}
        tid = {r["no"]: r["id"] for r in q("SELECT id,no FROM terminals")}
        with DB_LOCK:
            for s in seed["sample_sales"]:
                DB.execute("INSERT INTO daily_sales(day,branch_id,sys_cash,sys_net,act_cash,act_net,act_transfer,act_credit,returns_count,returns_value,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (d, bid[s["branch"]], s["sys_cash"], s["sys_net"], s["act_cash"], s["act_net"], s["act_transfer"], s["act_credit"], s["returns_count"], s["returns_value"], "استيراد الإكسل", now()))
            for t in seed["sample_terminals"]:
                DB.execute("INSERT INTO terminal_entries(day,terminal_id,amount,updated_by,updated_at) VALUES(?,?,?,?,?)", (d, tid[t["no"]], t["amount"], "استيراد الإكسل", now()))
            for v in seed["sample_variance"]:
                DB.execute("INSERT INTO variance_notes(day,company,branch_id,emp_no,emp_name,sys_rev,act_rev,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (d, v["company"], bid[v["branch"]], v["emp_no"], v["emp_name"], v["sys_rev"], v["act_rev"], "استيراد الإكسل", now()))
            DB.commit()
        print("تم استيراد بيانات يوم", d, "من ملف الإكسل كبيانات تجريبية.")


# ---------------------------------------------------------------- النسخ الاحتياطي
def make_backup(tag="auto"):
    name = f"revenue_{datetime.now():%Y%m%d_%H%M%S}_{tag}.db"
    dst = sqlite3.connect(os.path.join(BACKUP_DIR, name))
    with DB_LOCK:
        DB.backup(dst)
    dst.close()
    keep = int(os.environ.get("BACKUP_KEEP", 60))
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for f in files[:-keep]:
        os.remove(os.path.join(BACKUP_DIR, f))
    return name


def backup_loop():
    hours = float(os.environ.get("BACKUP_EVERY_HOURS", 6))
    while True:
        time.sleep(hours * 3600)
        try:
            make_backup("auto")
        except Exception as e:  # noqa
            print("backup failed:", e)


# ---------------------------------------------------------------- منطق الأعمال
class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        self.code, self.msg, self.extra = code, msg, extra or {}


def valid_day(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        raise ApiError(400, "تاريخ غير صالح")


def can(u, perm):
    return u["role"] == "admin" or perm in json.loads(u["perms"])


def check_branch(u, branch_id):
    if u["role"] == "admin":
        return
    ids = json.loads(u["branch_ids"])
    if ids and branch_id not in ids:
        raise ApiError(403, "هذا الفرع غير مخصص لك")


def check_day_editable(u, day):
    if u["role"] == "admin" or can(u, "edit_past"):
        return
    limit = date.today() - timedelta(days=int(get_setting("past_days", 2)))
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if d < limit:
        raise ApiError(403, "لا تملك صلاحية تعديل هذا اليوم (تجاوز الحد المسموح)")
    if d > date.today():
        raise ApiError(400, "لا يمكن التسجيل بتاريخ مستقبلي")


def num(v, name="القيمة"):
    try:
        x = round(float(v if v not in ("", None) else 0), 2)
    except Exception:
        raise ApiError(400, f"{name} غير رقمية")
    if abs(x) > 1e9:
        raise ApiError(400, f"{name} كبيرة جداً")
    return x


def conflict(entity, current):
    raise ApiError(409, "تم تعديل هذا السجل من مستخدم آخر قبل حفظك. تم تحميل آخر نسخة.", {"current": current, "entity": entity})


def classify(net_diff, thr):
    if net_diff >= thr:
        return "surplus_abnormal"
    if net_diff >= 0:
        return "surplus_normal"
    if net_diff > -thr:
        return "shortage_normal"
    return "shortage_abnormal"


def get_day(u, day):
    branches = q("SELECT * FROM branches WHERE active=1 ORDER BY sort,id")
    ids = json.loads(u["branch_ids"]) if u["role"] != "admin" else []
    visible = [b for b in branches if not ids or b["id"] in ids]
    vid = {b["id"] for b in visible}
    sales = {r["branch_id"]: r for r in q("SELECT * FROM daily_sales WHERE day=?", (day,)) if r["branch_id"] in vid}
    terms = q("SELECT * FROM terminals WHERE active=1 ORDER BY sort,id")
    tent = {r["terminal_id"]: r for r in q("SELECT * FROM terminal_entries WHERE day=?", (day,))}
    variance = [r for r in q("SELECT v.*, b.name AS branch FROM variance_notes v JOIN branches b ON b.id=v.branch_id WHERE v.day=? ORDER BY v.id", (day,)) if r["branch_id"] in vid]
    tasks = [r for r in q("SELECT * FROM task_status WHERE day=?", (day,)) if r["branch_id"] in vid]
    return {"day": day, "branches": visible, "sales": list(sales.values()),
            "terminals": [t for t in terms if t["branch_id"] in vid],
            "terminal_entries": [tent[t["id"]] for t in terms if t["id"] in tent and t["branch_id"] in vid],
            "variance": variance, "tasks": tasks, "server_time": now()}


def put_sales(u, body):
    if not can(u, "sales"):
        raise ApiError(403, "لا تملك صلاحية تسجيل الإيرادات")
    day, bid = valid_day(body.get("day", "")), int(body.get("branch_id", 0))
    check_branch(u, bid)
    check_day_editable(u, day)
    if not q1("SELECT 1 FROM branches WHERE id=? AND active=1", (bid,)):
        raise ApiError(404, "فرع غير موجود")
    vals = {f: num(body.get(f), f) for f in SALES_FIELDS}
    vals["returns_count"] = int(vals["returns_count"])
    notes = str(body.get("notes", ""))[:500]
    with DB_LOCK:
        cur = q1("SELECT * FROM daily_sales WHERE day=? AND branch_id=?", (day, bid))
        if cur is None:
            if body.get("version"):
                conflict("sales", None)
            DB.execute("INSERT INTO daily_sales(day,branch_id,%s,notes,updated_by,updated_at) VALUES(?,?,%s,?,?,?)" % (",".join(SALES_FIELDS), ",".join("?" * len(SALES_FIELDS))),
                       (day, bid, *[vals[f] for f in SALES_FIELDS], notes, u["username"], now()))
            audit(u["username"], "create", "sales", day, bid, None, {**vals, "notes": notes})
        else:
            if int(body.get("version", 0)) != cur["version"]:
                conflict("sales", cur)
            DB.execute("UPDATE daily_sales SET %s, notes=?, version=version+1, updated_by=?, updated_at=? WHERE day=? AND branch_id=?" % ",".join(f + "=?" for f in SALES_FIELDS),
                       (*[vals[f] for f in SALES_FIELDS], notes, u["username"], now(), day, bid))
            old = {f: cur[f] for f in SALES_FIELDS + ["notes"]}
            audit(u["username"], "update", "sales", day, bid, old, {**vals, "notes": notes})
        # التسجيل على النظام: يُحدَّث تلقائياً إلى "قيد الإنجاز" إن لم تكن المهمة مكتملة
        _auto_task(u, day, bid, "register", "progress")
        DB.commit()
    return q1("SELECT * FROM daily_sales WHERE day=? AND branch_id=?", (day, bid))


def _auto_task(u, day, bid, task, status):
    cur = q1("SELECT * FROM task_status WHERE day=? AND branch_id=? AND task=?", (day, bid, task))
    if cur is None:
        DB.execute("INSERT INTO task_status(day,branch_id,task,status,updated_by,updated_at) VALUES(?,?,?,?,?,?)", (day, bid, task, status, u["username"], now()))
    elif cur["status"] in ("pending", "late"):
        DB.execute("UPDATE task_status SET status=?, version=version+1, updated_by=?, updated_at=? WHERE day=? AND branch_id=? AND task=?", (status, u["username"], now(), day, bid, task))


def put_terminal(u, body):
    if not can(u, "terminals"):
        raise ApiError(403, "لا تملك صلاحية تسجيل سحب الشبكات")
    day, tid = valid_day(body.get("day", "")), int(body.get("terminal_id", 0))
    t = q1("SELECT * FROM terminals WHERE id=? AND active=1", (tid,))
    if not t:
        raise ApiError(404, "جهاز غير موجود")
    check_branch(u, t["branch_id"])
    check_day_editable(u, day)
    amount, note = num(body.get("amount"), "المبلغ"), str(body.get("note", ""))[:300]
    with DB_LOCK:
        cur = q1("SELECT * FROM terminal_entries WHERE day=? AND terminal_id=?", (day, tid))
        if cur is None:
            if body.get("version"):
                conflict("terminal", None)
            DB.execute("INSERT INTO terminal_entries(day,terminal_id,amount,note,updated_by,updated_at) VALUES(?,?,?,?,?,?)", (day, tid, amount, note, u["username"], now()))
            audit(u["username"], "create", "terminal", day, t["no"], None, {"amount": amount, "note": note})
        else:
            if int(body.get("version", 0)) != cur["version"]:
                conflict("terminal", cur)
            DB.execute("UPDATE terminal_entries SET amount=?, note=?, version=version+1, updated_by=?, updated_at=? WHERE day=? AND terminal_id=?", (amount, note, u["username"], now(), day, tid))
            audit(u["username"], "update", "terminal", day, t["no"], {"amount": cur["amount"], "note": cur["note"]}, {"amount": amount, "note": note})
        DB.commit()
    return q1("SELECT * FROM terminal_entries WHERE day=? AND terminal_id=?", (day, tid))


def save_variance(u, body, vid=None):
    if not can(u, "variance"):
        raise ApiError(403, "لا تملك صلاحية تسجيل ملاحظات العجز والفائض")
    day, bid = valid_day(body.get("day", "")), int(body.get("branch_id", 0))
    check_branch(u, bid)
    check_day_editable(u, day)
    f = {"company": str(body.get("company", ""))[:100], "emp_no": str(body.get("emp_no", ""))[:30], "emp_name": str(body.get("emp_name", ""))[:100],
         "emp_title": str(body.get("emp_title", ""))[:100], "sys_rev": num(body.get("sys_rev")), "act_rev": num(body.get("act_rev")), "notes": str(body.get("notes", ""))[:1000]}
    with DB_LOCK:
        if vid is None:
            DB.execute("INSERT INTO variance_notes(day,branch_id,company,emp_no,emp_name,emp_title,sys_rev,act_rev,notes,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (day, bid, f["company"], f["emp_no"], f["emp_name"], f["emp_title"], f["sys_rev"], f["act_rev"], f["notes"], u["username"], now()))
            vid = DB.execute("SELECT last_insert_rowid()").fetchone()[0]
            audit(u["username"], "create", "variance", day, vid, None, f)
        else:
            cur = q1("SELECT * FROM variance_notes WHERE id=?", (vid,))
            if not cur:
                raise ApiError(404, "السجل غير موجود (ربما حُذف)")
            check_branch(u, cur["branch_id"])
            if int(body.get("version", 0)) != cur["version"]:
                conflict("variance", cur)
            DB.execute("UPDATE variance_notes SET branch_id=?,company=?,emp_no=?,emp_name=?,emp_title=?,sys_rev=?,act_rev=?,notes=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                       (bid, f["company"], f["emp_no"], f["emp_name"], f["emp_title"], f["sys_rev"], f["act_rev"], f["notes"], u["username"], now(), vid))
            audit(u["username"], "update", "variance", day, vid, {k: cur[k] for k in f}, f)
        DB.commit()
    return q1("SELECT v.*, b.name AS branch FROM variance_notes v JOIN branches b ON b.id=v.branch_id WHERE v.id=?", (vid,))


def delete_variance(u, vid):
    if not can(u, "variance"):
        raise ApiError(403, "غير مسموح")
    with DB_LOCK:
        cur = q1("SELECT * FROM variance_notes WHERE id=?", (vid,))
        if not cur:
            return {"ok": True}
        check_branch(u, cur["branch_id"])
        check_day_editable(u, cur["day"])
        DB.execute("DELETE FROM variance_notes WHERE id=?", (vid,))
        audit(u["username"], "delete", "variance", cur["day"], vid, cur, None)
        DB.commit()
    return {"ok": True}


def put_task(u, body):
    if not can(u, "tasks"):
        raise ApiError(403, "لا تملك صلاحية تحديث المهام")
    day, bid, task = valid_day(body.get("day", "")), int(body.get("branch_id", 0)), body.get("task")
    status = body.get("status", "pending")
    if task not in TASK_KEYS or status not in STATUSES:
        raise ApiError(400, "مهمة أو حالة غير صالحة")
    check_branch(u, bid)
    check_day_editable(u, day)
    note = str(body.get("note", ""))[:500]
    with DB_LOCK:
        cur = q1("SELECT * FROM task_status WHERE day=? AND branch_id=? AND task=?", (day, bid, task))
        if cur is None:
            if body.get("version"):
                conflict("task", None)
            DB.execute("INSERT INTO task_status(day,branch_id,task,status,note,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (day, bid, task, status, note, u["username"], now()))
            audit(u["username"], "create", "task", day, f"{bid}:{task}", None, {"status": status, "note": note})
        else:
            if int(body.get("version", 0)) != cur["version"]:
                conflict("task", cur)
            DB.execute("UPDATE task_status SET status=?,note=?,version=version+1,updated_by=?,updated_at=? WHERE day=? AND branch_id=? AND task=?", (status, note, u["username"], now(), day, bid, task))
            audit(u["username"], "update", "task", day, f"{bid}:{task}", {"status": cur["status"], "note": cur["note"]}, {"status": status, "note": note})
        DB.commit()
    return q1("SELECT * FROM task_status WHERE day=? AND branch_id=? AND task=?", (day, bid, task))


def checklist(u, month):
    """جدول المتابعة اليومي لمدير الحسابات: لكل يوم من الشهر حالة كل مهمة مجمّعة على الفروع."""
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    branches = q("SELECT id FROM branches WHERE active=1")
    n = len(branches)
    rows = q("SELECT day,task,status,COUNT(*) c FROM task_status WHERE day BETWEEN ? AND ? GROUP BY day,task,status", (first.isoformat(), last.isoformat()))
    agg = {}
    for r in rows:
        agg.setdefault(r["day"], {}).setdefault(r["task"], {})[r["status"]] = r["c"]
    notes = q("SELECT day,task,COUNT(*) c FROM task_status WHERE day BETWEEN ? AND ? AND note<>'' GROUP BY day,task", (first.isoformat(), last.isoformat()))
    nmap = {(r["day"], r["task"]): r["c"] for r in notes}
    sales_cnt = {r["day"]: r["c"] for r in q("SELECT day,COUNT(*) c FROM daily_sales WHERE day BETWEEN ? AND ? GROUP BY day", (first.isoformat(), last.isoformat()))}
    today = date.today()
    deadline_hour = int(get_setting("deadline_hour", 18))
    out = []
    d = first
    while d <= min(last, today):
        iso = d.isoformat()
        overdue = d < today or (d == today and datetime.now().hour >= deadline_hour)
        cells = {}
        for k in TASK_KEYS:
            c = agg.get(iso, {}).get(k, {})
            done, prog = c.get("done", 0), c.get("progress", 0)
            late_explicit = c.get("late", 0)
            if done >= n:
                st = "done"
            elif overdue:
                st = "late"
            elif done or prog:
                st = "progress"
            else:
                st = "pending"
            if late_explicit and st != "done":
                st = "late"
            cells[k] = {"status": st, "done": done, "progress": prog, "late": late_explicit, "total": n, "notes": nmap.get((iso, k), 0)}
        out.append({"day": iso, "tasks": cells, "sales_rows": sales_cnt.get(iso, 0)})
        d += timedelta(days=1)
    return {"month": month, "branches": n, "days": list(reversed(out)), "tasks": [{"key": k, "label": l} for k, l in TASKS]}


def export_csv(u, day):
    d = get_day(u, day)
    thr = get_setting("threshold", 7)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["المبيعات اليومية", day])
    w.writerow(["م", "المنشأة", "الفرع", "نقدي-نظام", "شبكة-نظام", "إجمالي-نظام", "نقدي-فعلي", "شبكة-فعلي", "تحويل بنكي", "آجل", "إجمالي-فعلي", "فرق نقدي", "فرق شبكة", "صافي الفرق", "التصنيف", "عدد فواتير المرتجع", "قيمة المرتجع", "ملاحظات", "آخر تعديل بواسطة"])
    s = {r["branch_id"]: r for r in d["sales"]}
    names = {"surplus_normal": "فائض طبيعي", "surplus_abnormal": "فائض غير طبيعي", "shortage_normal": "عجز طبيعي", "shortage_abnormal": "عجز غير طبيعي"}
    for i, b in enumerate(d["branches"], 1):
        r = s.get(b["id"])
        if not r:
            w.writerow([i, b["company"], b["name"]])
            continue
        a_tot = r["act_cash"] + r["act_net"] + r["act_transfer"] + r["act_credit"]
        lc = round(r["act_cash"] - r["sys_cash"], 2)
        ln = round(r["act_net"] + r["act_transfer"] + r["act_credit"] - r["sys_net"], 2)
        net = round(lc + ln, 2)
        w.writerow([i, b["company"], b["name"], r["sys_cash"], r["sys_net"], round(r["sys_cash"] + r["sys_net"], 2), r["act_cash"], r["act_net"], r["act_transfer"], r["act_credit"], round(a_tot, 2), lc, ln, net, names[classify(net, thr)], r["returns_count"], r["returns_value"], r["notes"], r["updated_by"]])
    w.writerow([])
    w.writerow(["سحب الشبكات"])
    w.writerow(["الفرع", "رقم الجهاز", "القيمة", "ملاحظات"])
    bn = {b["id"]: b["name"] for b in d["branches"]}
    te = {e["terminal_id"]: e for e in d["terminal_entries"]}
    for t in d["terminals"]:
        e = te.get(t["id"])
        w.writerow([bn[t["branch_id"]], t["no"], e["amount"] if e else "", e["note"] if e else ""])
    w.writerow([])
    w.writerow(["العجز والفائض غير الطبيعي"])
    w.writerow(["المنشأة", "الفرع", "الرقم الوظيفي", "الاسم", "المسمى", "إيراد النظام", "الإيراد الفعلي", "الفرق", "ملاحظات"])
    for v in d["variance"]:
        w.writerow([v["company"], v["branch"], v["emp_no"], v["emp_name"], v["emp_title"], v["sys_rev"], v["act_rev"], round(v["act_rev"] - v["sys_rev"], 2), v["notes"]])
    return ("﻿" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- الإدارة
def public_user(r):
    return {"id": r["id"], "username": r["username"], "full_name": r["full_name"], "role": r["role"], "perms": json.loads(r["perms"]),
            "branch_ids": json.loads(r["branch_ids"]), "active": bool(r["active"]), "must_change": bool(r["must_change"])}


def validate_password(pw):
    if len(pw or "") < 8:
        raise ApiError(400, "كلمة المرور يجب ألا تقل عن 8 أحرف")


def admin_users(u, method, uid, body):
    if method == "GET":
        return [public_user(r) for r in q("SELECT * FROM users ORDER BY id")]
    if method == "POST":
        un, fn, pw = str(body.get("username", "")).strip().lower(), str(body.get("full_name", "")).strip(), body.get("password", "")
        if not un or not fn:
            raise ApiError(400, "اسم المستخدم والاسم الكامل مطلوبان")
        validate_password(pw)
        if q1("SELECT 1 FROM users WHERE username=?", (un,)):
            raise ApiError(400, "اسم المستخدم مستخدم مسبقاً")
        perms = [p for p in body.get("perms", []) if p in PERMS]
        create_user(un, fn, pw, "accountant", perms, [int(x) for x in body.get("branch_ids", [])], 1)
        audit(u["username"], "create", "user", None, un, None, {"perms": perms, "branch_ids": body.get("branch_ids", [])})
        DB.commit()
        return public_user(q1("SELECT * FROM users WHERE username=?", (un,)))
    cur = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not cur:
        raise ApiError(404, "مستخدم غير موجود")
    if method == "PUT":
        fn = str(body.get("full_name", cur["full_name"])).strip() or cur["full_name"]
        perms = [p for p in body.get("perms", json.loads(cur["perms"])) if p in PERMS]
        bids = [int(x) for x in body.get("branch_ids", json.loads(cur["branch_ids"]))]
        active = 1 if body.get("active", cur["active"]) else 0
        if cur["role"] == "admin":
            perms, bids, active = list(PERMS), [], 1
        with DB_LOCK:
            DB.execute("UPDATE users SET full_name=?,perms=?,branch_ids=?,active=? WHERE id=?", (fn, json.dumps(perms), json.dumps(bids), active, uid))
            if body.get("password"):
                validate_password(body["password"])
                salt = secrets.token_hex(16)
                DB.execute("UPDATE users SET pw_hash=?,salt=?,must_change=1 WHERE id=?", (hash_pw(body["password"], salt), salt, uid))
            if not active:
                DB.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            audit(u["username"], "update", "user", None, cur["username"], {"perms": json.loads(cur["perms"]), "branch_ids": json.loads(cur["branch_ids"]), "active": cur["active"]},
                  {"perms": perms, "branch_ids": bids, "active": active, "password_reset": bool(body.get("password"))})
            DB.commit()
        return public_user(q1("SELECT * FROM users WHERE id=?", (uid,)))
    raise ApiError(405, "طريقة غير مدعومة")


def admin_branches(u, method, body):
    if method == "POST":
        name, company = str(body.get("name", "")).strip(), str(body.get("company", "")).strip()
        if not name or not company:
            raise ApiError(400, "اسم الفرع والمنشأة مطلوبان")
        with DB_LOCK:
            mx = DB.execute("SELECT COALESCE(MAX(sort),0) FROM branches").fetchone()[0]
            try:
                DB.execute("INSERT INTO branches(company,name,sort) VALUES(?,?,?)", (company, name, mx + 1))
            except sqlite3.IntegrityError:
                raise ApiError(400, "الفرع موجود مسبقاً")
            audit(u["username"], "create", "branch", None, name, None, {"company": company})
            DB.commit()
        return {"ok": True}
    bid = int(body.get("id", 0))
    with DB_LOCK:
        DB.execute("UPDATE branches SET company=COALESCE(?,company), name=COALESCE(?,name), active=COALESCE(?,active) WHERE id=?",
                   (body.get("company"), body.get("name"), (1 if body["active"] else 0) if "active" in body else None, bid))
        audit(u["username"], "update", "branch", None, bid, None, body)
        DB.commit()
    return {"ok": True}


def admin_terminals(u, body):
    if body.get("action") == "add":
        bid, no = int(body.get("branch_id", 0)), str(body.get("no", "")).strip()
        if not no:
            raise ApiError(400, "رقم الجهاز مطلوب")
        with DB_LOCK:
            mx = DB.execute("SELECT COALESCE(MAX(sort),0) FROM terminals").fetchone()[0]
            try:
                DB.execute("INSERT INTO terminals(branch_id,no,sort) VALUES(?,?,?)", (bid, no, mx + 1))
            except sqlite3.IntegrityError:
                raise ApiError(400, "رقم الجهاز مسجل مسبقاً")
            audit(u["username"], "create", "terminal_def", None, no, None, {"branch_id": bid})
            DB.commit()
    else:
        with DB_LOCK:
            DB.execute("UPDATE terminals SET active=? WHERE id=?", (1 if body.get("active") else 0, int(body.get("id", 0))))
            audit(u["username"], "update", "terminal_def", None, body.get("id"), None, body)
            DB.commit()
    return {"ok": True}


# ---------------------------------------------------------------- خادم HTTP
LOGIN_FAILS = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "Revenue/1.0"

    def log_message(self, fmt, *a):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # --- أدوات
    def send_json(self, obj, code=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def current_user(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "sid" not in c:
            return None
        s = q1("SELECT * FROM sessions WHERE token=? AND expires>?", (c["sid"].value, time.time()))
        if not s:
            return None
        u = q1("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],))
        return u

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ApiError(413, "حجم الطلب كبير")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            raise ApiError(400, "JSON غير صالح")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        p = urlparse(self.path)
        try:
            if p.path.startswith("/api/"):
                if method != "GET" and self.headers.get("X-Requested-With") != "fetch":  # حماية CSRF
                    raise ApiError(403, "طلب مرفوض")
                self.api(method, p.path[5:].strip("/"), parse_qs(p.query))
            elif method == "GET":
                self.static(p.path)
            else:
                raise ApiError(405, "غير مدعوم")
        except ApiError as e:
            self.send_json({"error": e.msg, **e.extra}, e.code)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self.send_json({"error": "خطأ داخلي في الخادم"}, 500)

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        self.send_bytes(open(fp, "rb").read(), ctype + ("; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else ""),
                        {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    def api(self, method, path, qs):
        a = lambda k, d="": qs.get(k, [d])[0]  # noqa
        if path == "login" and method == "POST":
            b = self.body()
            un = str(b.get("username", "")).strip().lower()
            ip = self.client_address[0]
            fails = [t for t in LOGIN_FAILS.get((ip, un), []) if t > time.time() - 600]
            if len(fails) >= 8:
                raise ApiError(429, "محاولات كثيرة. حاول بعد 10 دقائق")
            u = q1("SELECT * FROM users WHERE username=? AND active=1", (un,))
            if not u or not hmac.compare_digest(hash_pw(str(b.get("password", "")), u["salt"]), u["pw_hash"]):
                LOGIN_FAILS[(ip, un)] = fails + [time.time()]
                raise ApiError(401, "اسم المستخدم أو كلمة المرور غير صحيحة")
            LOGIN_FAILS.pop((ip, un), None)
            tok = secrets.token_urlsafe(32)
            with DB_LOCK:
                DB.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
                DB.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], time.time() + SESSION_HOURS * 3600))
                audit(u["username"], "login", "session")
                DB.commit()
            ck = f"sid={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
            return self.send_json(public_user(u), headers={"Set-Cookie": ck})
        u = self.current_user()
        if path == "logout" and method == "POST":
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if "sid" in c:
                with DB_LOCK:
                    DB.execute("DELETE FROM sessions WHERE token=?", (c["sid"].value,))
                    DB.commit()
            return self.send_json({"ok": True}, headers={"Set-Cookie": "sid=; Path=/; Max-Age=0"})
        if not u:
            raise ApiError(401, "يرجى تسجيل الدخول")
        if path == "me":
            return self.send_json(public_user(u))
        if path == "change-password" and method == "POST":
            b = self.body()
            if not hmac.compare_digest(hash_pw(str(b.get("old", "")), u["salt"]), u["pw_hash"]):
                raise ApiError(400, "كلمة المرور الحالية غير صحيحة")
            validate_password(b.get("new"))
            salt = secrets.token_hex(16)
            with DB_LOCK:
                DB.execute("UPDATE users SET pw_hash=?,salt=?,must_change=0 WHERE id=?", (hash_pw(b["new"], salt), salt, u["id"]))
                audit(u["username"], "password", "user", None, u["username"])
                DB.commit()
            return self.send_json({"ok": True})
        if u["must_change"]:
            raise ApiError(403, "يجب تغيير كلمة المرور أولاً")
        if path == "meta":
            return self.send_json({"tasks": [{"key": k, "label": l} for k, l in TASKS], "perms": PERMS, "settings": {"threshold": get_setting("threshold", 7), "past_days": get_setting("past_days", 2), "deadline_hour": get_setting("deadline_hour", 18)},
                                   "all_branches": q("SELECT * FROM branches ORDER BY sort,id") if u["role"] == "admin" else [], "all_terminals": q("SELECT * FROM terminals ORDER BY sort,id") if u["role"] == "admin" else [],
                                   "today": date.today().isoformat()})
        if path == "day" and method == "GET":
            return self.send_json(get_day(u, valid_day(a("date"))))
        if path == "sales" and method == "PUT":
            return self.send_json(put_sales(u, self.body()))
        if path == "terminal" and method == "PUT":
            return self.send_json(put_terminal(u, self.body()))
        if path == "variance" and method == "POST":
            return self.send_json(save_variance(u, self.body()))
        if path.startswith("variance/"):
            vid = int(path.split("/")[1])
            if method == "PUT":
                return self.send_json(save_variance(u, self.body(), vid))
            if method == "DELETE":
                return self.send_json(delete_variance(u, vid))
        if path == "task" and method == "PUT":
            return self.send_json(put_task(u, self.body()))
        if path == "checklist":
            m = a("month", date.today().strftime("%Y-%m"))
            return self.send_json(checklist(u, m))
        if path == "history":
            where, args = [], []
            if a("date"):
                where.append("day=?"); args.append(valid_day(a("date")))
            if a("entity"):
                where.append("entity=?"); args.append(a("entity"))
            if a("user"):
                where.append("username=?"); args.append(a("user"))
            if u["role"] != "admin":
                where.append("(username=? OR entity IN ('sales','terminal','variance','task'))"); args.append(u["username"])
            sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
            rows = q(sql, (*args, min(int(a("limit", 200)), 1000)))
            return self.send_json(rows)
        if path == "export":
            if not can(u, "export"):
                raise ApiError(403, "لا تملك صلاحية التصدير")
            day = valid_day(a("date"))
            return self.send_bytes(export_csv(u, day), "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="revenue_{day}.csv"'})
        if path == "monthly":
            m = a("month", date.today().strftime("%Y-%m"))
            return self.send_json(q("""SELECT day, SUM(sys_cash+sys_net) sys_total, SUM(act_cash+act_net+act_transfer+act_credit) act_total,
                    SUM(act_cash+act_net+act_transfer+act_credit-sys_cash-sys_net) diff, COUNT(*) branches FROM daily_sales WHERE day LIKE ? GROUP BY day ORDER BY day DESC""", (m + "-%",)))
        # ------- مدير الحسابات فقط
        if u["role"] != "admin":
            raise ApiError(403, "للمدير فقط")
        if path == "users":
            return self.send_json(admin_users(u, method, None, self.body() if method != "GET" else {}))
        if path.startswith("users/"):
            return self.send_json(admin_users(u, method, int(path.split("/")[1]), self.body()))
        if path == "branches":
            return self.send_json(admin_branches(u, method, self.body()))
        if path == "terminals":
            return self.send_json(admin_terminals(u, self.body()))
        if path == "settings" and method == "PUT":
            b = self.body()
            for k, lo, hi in (("threshold", 0, 1e6), ("past_days", 0, 365), ("deadline_hour", 0, 23)):
                if k in b:
                    set_setting(k, max(lo, min(hi, num(b[k]))))
            audit(u["username"], "update", "settings", None, None, None, b)
            DB.commit()
            return self.send_json({"ok": True})
        if path == "backups":
            if method == "POST":
                name = make_backup("manual")
                audit(u["username"], "backup", "backup", None, name)
                DB.commit()
                return self.send_json({"name": name})
            files = sorted(os.listdir(BACKUP_DIR), reverse=True)
            return self.send_json([{"name": f, "size": os.path.getsize(os.path.join(BACKUP_DIR, f)), "time": datetime.fromtimestamp(os.path.getmtime(os.path.join(BACKUP_DIR, f))).strftime("%Y-%m-%d %H:%M:%S")} for f in files if f.endswith(".db")])
        if path.startswith("backups/") and method == "GET":
            name = os.path.basename(path.split("/", 1)[1])
            fp = os.path.join(BACKUP_DIR, name)
            if not os.path.isfile(fp):
                raise ApiError(404, "غير موجود")
            return self.send_bytes(open(fp, "rb").read(), "application/octet-stream", {"Content-Disposition": f'attachment; filename="{name}"'})
        raise ApiError(404, "مسار غير موجود")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--seed-sample", action="store_true", help="استيراد بيانات يوم الإكسل المرفق كبيانات تجريبية")
    args = ap.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db(args.seed_sample)
    threading.Thread(target=backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"النظام يعمل على http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
