# app.py — Flask privé avec code d'accès + SQLite persistant
import os
from flask import Flask, request, redirect, url_for, render_template_string, flash, jsonify, Response, session
import sqlite3, io, csv
from pathlib import Path
from functools import wraps

# ----- Config via variables d'environnement -----
ACCESS_CODE = os.environ.get("ACCESS_CODE", "change-me")          # code à partager à l'équipe
SECRET_KEY  = os.environ.get("SECRET_KEY",  "please-change-this")  # secret pour sessions
DB_PATH     = os.environ.get("DB_PATH", "data.sqlite")             # où stocker la base

APP_DB = Path(DB_PATH)
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ----- Auth minimale (écran code d'accès) -----
EXEMPT_PATHS = {"/login", "/health"}
def require_login(f):
    @wraps(f)
    def w(*a, **k):
        if request.path in EXEMPT_PATHS or request.path.startswith("/static/"):
            return f(*a, **k)
        if session.get("ok"):
            return f(*a, **k)
        return redirect(url_for("login", next=request.full_path))
    return w

@app.get("/login")
def login():
    tpl = """
    <!doctype html><meta charset="utf-8"><title>Connexion</title>
    <style>body{font-family:system-ui;background:#0f172a;color:#e5e7eb;display:grid;place-items:center;height:100vh}
    .card{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:24px;min-width:320px}
    input{width:100%;padding:10px 12px;border:1px solid #334155;background:#0b1220;color:#e5e7eb;border-radius:10px}
    button{margin-top:10px;padding:10px 14px;border:none;border-radius:10px;background:#22c55e;color:white;cursor:pointer}
    .msg{color:#fca5a5;margin-top:8px}</style>
    <div class="card">
      <h2>🔒 Accès réservé</h2>
      <form method="post" action="{{ url_for('login_post') }}">
        <input type="password" name="code" placeholder="Code d'accès" autofocus required>
        <button type="submit">Entrer</button>
      </form>
      {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
    </div>"""
    return render_template_string(tpl, msg=request.args.get("msg"))

@app.post("/login")
def login_post():
    if (request.form.get("code") or "").strip() == ACCESS_CODE:
        session["ok"] = True
        return redirect(request.args.get("next") or url_for("index"))
    return redirect(url_for("login", msg="Code incorrect"))

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.get("/health")
def health(): return "ok", 200

# ----- DB util -----
def get_db():
    APP_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(APP_DB); c.row_factory = sqlite3.Row; return c

def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS people(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        last_name  TEXT NOT NULL,
        first_name TEXT NOT NULL,
        address    TEXT NOT NULL,
        phone      TEXT NOT NULL,
        specialty  TEXT NOT NULL
      )""")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_person_v2
                   ON people(LOWER(last_name), LOWER(first_name), LOWER(specialty))""")
    conn.commit(); conn.close()

# ----- Features (CRUD / recherche / stats / CSV / auto-complétion) -----
def fetch_rows(last_name_q, spec_q):
    conn=get_db(); cur=conn.cursor()
    sql="SELECT * FROM people WHERE 1=1"; p=[]
    if last_name_q: sql+=" AND LOWER(last_name) LIKE ?"; p.append(f"%{last_name_q.lower()}%")
    if spec_q:      sql+=" AND LOWER(specialty) LIKE ?"; p.append(f"%{spec_q.lower()}%")
    sql+=" ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"
    cur.execute(sql,p); rows=cur.fetchall(); conn.close(); return rows

def stats_per_specialty():
    conn=get_db(); cur=conn.cursor()
    cur.execute("""SELECT specialty, COUNT(*) n FROM people
                   GROUP BY LOWER(specialty), specialty
                   ORDER BY n DESC, specialty COLLATE NOCASE""")
    per=cur.fetchall(); cur.execute("SELECT COUNT(*) FROM people"); total=cur.fetchone()[0]
    conn.close(); return per,total

def suggest(field, q):
    if field not in ("last_name","specialty"): return []
    q=(q or "").strip()
    if len(q)<3: return []
    conn=get_db(); cur=conn.cursor()
    cur.execute(f"""SELECT DISTINCT {field} FROM people
                    WHERE LOWER({field}) LIKE ? ORDER BY {field} COLLATE NOCASE LIMIT 10""",
                (q.lower()+"%",))
    out=[r[0] for r in cur.fetchall()]; conn.close(); return out

@app.get("/")
@require_login
def index():
    name_q=(request.args.get("name") or "").strip()
    spec_q=(request.args.get("specialty") or "").strip()
    rows=fetch_rows(name_q,spec_q); per,total=stats_per_specialty()
    return render_template_string(TPL, rows=rows, name_q=name_q, spec_q=spec_q,
                                  per_spec=per, total=total, edit_item=None)

@app.post("/create")
@require_login
def create():
    f=lambda k:(request.form.get(k) or "").strip()
    last,first,addr,phone,spec = f("last_name"),f("first_name"),f("address"),f("phone"),f("specialty")
    if not all([last,first,addr,phone,spec]):
        flash("Tous les champs sont obligatoires.","error"); return redirect(url_for("index"))
    try:
        conn=get_db(); cur=conn.cursor()
        cur.execute("""INSERT INTO people(last_name,first_name,address,phone,specialty)
                       VALUES(?,?,?,?,?)""",(last,first,addr,phone,spec))
        conn.commit(); flash("Créé avec succès ✅","success")
    except sqlite3.IntegrityError:
        flash("Doublon : même Nom + Prénom + Spécialité.","error")
    finally:
        conn.close()
    return redirect(url_for("index", name=request.args.get("name",""), specialty=request.args.get("specialty","")))

@app.get("/edit/<int:item_id>")
@require_login
def edit(item_id):
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT * FROM people WHERE id=?", (item_id,)); row=cur.fetchone()
    cur.execute("SELECT * FROM people ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"); rows=cur.fetchall()
    per,total=stats_per_specialty(); conn.close()
    if not row: flash("Élément introuvable.","error"); return redirect(url_for("index"))
    return render_template_string(TPL, rows=rows, name_q="", spec_q="", per_spec=per, total=total, edit_item=row)

@app.post("/update/<int:item_id>")
@require_login
def update(item_id):
    f=lambda k:(request.form.get(k) or "").strip()
    last,first,addr,phone,spec = f("last_name"),f("first_name"),f("address"),f("phone"),f("specialty")
    if not all([last,first,addr,phone,spec]):
        flash("Tous les champs sont obligatoires.","error"); return redirect(url_for("edit", item_id=item_id))
    try:
        conn=get_db(); cur=conn.cursor()
        cur.execute("""UPDATE people SET last_name=?, first_name=?, address=?, phone=?, specialty=?
                       WHERE id=?""",(last,first,addr,phone,spec,item_id))
        conn.commit(); flash("Mis à jour ✏️","success")
    except sqlite3.IntegrityError:
        flash("Doublon : même Nom + Prénom + Spécialité.","error"); return redirect(url_for("edit", item_id=item_id))
    finally:
        conn.close()
    return redirect(url_for("index"))

@app.post("/delete/<int:item_id>")
@require_login
def delete(item_id):
    conn=get_db(); cur=conn.cursor()
    cur.execute("DELETE FROM people WHERE id=?", (item_id,)); conn.commit(); conn.close()
    flash("Supprimé 🗑️","success"); return redirect(url_for("index"))

@app.get("/api/suggest/name")
@require_login
def api_suggest_name(): return jsonify(suggest("last_name", request.args.get("q","")))

@app.get("/api/suggest/specialty")
@require_login
def api_suggest_specialty(): return jsonify(suggest("specialty", request.args.get("q","")))

@app.get("/export.csv")
@require_login
def export_csv():
    rows = fetch_rows((request.args.get("name") or "").strip(),
                      (request.args.get("specialty") or "").strip())
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["id","nom","prénom","adresse","téléphone","spécialité"])
    for r in rows: w.writerow([r["id"], r["last_name"], r["first_name"], r["address"], r["phone"], r["specialty"]])
    data = out.getvalue().encode("utf-8-sig")
    return Response(data, mimetype="text/csv; charset=utf-8",
      headers={"Content-Disposition": 'attachment; filename="export.csv"'})

# ----- Template (UI) -----
TPL = """
<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fiches — Nom/Prénom/Adresse/Téléphone/Spécialité</title>
<style>
:root{--bg:#0f172a;--card:#111827;--muted:#94a3b8;--txt:#e5e7eb;--focus:#38bdf8;--danger:#ef4444;--ok:#22c55e;}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--txt)}
.container{max-width:1100px;margin:28px auto;padding:0 16px}
.card{background:var(--card);border:1px solid #1f2937;border-radius:16px;padding:20px}
h1{margin:0 0 14px;font-size:26px}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
label{font-size:12px;color:var(--muted);display:block;margin-bottom:4px}
input[type=text],input[type=tel]{padding:10px 12px;border:1px solid #334155;background:#0b1220;color:#e5e7eb;border-radius:10px;outline:none;width:240px}
input:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(56,189,248,.2)}
button{padding:10px 14px;border:1px solid #334155;background:#0b1220;color:#e5e7eb;border-radius:10px;cursor:pointer}
button.primary{background:linear-gradient(90deg,#16a34a,#22c55e);border:none;color:white}
button.danger{background:#7f1d1d;border-color:#7f1d1d;color:white}
.muted{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:separate;border-spacing:0 10px;margin-top:12px}
th,td{text-align:left;padding:10px 12px}
thead th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.6px}
tbody tr{background:#0b1220;border:1px solid #1f2937}
tbody tr td:first-child{border-radius:12px 0 0 12px}
tbody tr td:last-child{border-radius:0 12px 12px 0}
.right{text-align:right}.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:980px){.grid{grid-template-columns:1.15fr .85fr}}
.dropdown{position:relative}.list{position:absolute;top:100%;left:0;right:0;background:#0b1220;border:1px solid #334155;border-radius:10px;max-height:220px;overflow:auto;display:none;z-index:10}
.list button{display:block;width:100%;text-align:left;border:none;background:transparent;padding:8px 12px}
.list button:hover{background:#111827}
.flash{margin-bottom:12px;padding:10px 12px;border-radius:10px}
.flash.success{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.4)}
.flash.error{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.4)}
.chip{display:inline-block;padding:6px 10px;border-radius:999px;border:1px solid #334155;background:#0b1220;font-size:12px}
.small{font-size:12px;color:var(--muted)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style></head><body>
<div class="container">
  <h1>📋 Fiches (accès privé)</h1>
  <p class="muted">Connecté. <a href="{{ url_for('logout') }}">Se déconnecter</a></p>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}{% for cat, msg in messages %}
      <div class="flash {{cat}}">{{ msg }}</div>
    {% endfor %}{% endif %}
  {% endwith %}

  <div class="grid">
    <div class="card">
      <h3 style="margin-top:0">🔎 Recherche</h3>
      <form class="inline" method="get" action="{{ url_for('index') }}">
        <div class="dropdown">
          <label for="name">Nom</label>
          <input type="text" id="name" name="name" value="{{ name_q }}" placeholder="ex: El Amrani" autocomplete="off">
          <div id="nameList" class="list"></div>
        </div>
        <div class="dropdown">
          <label for="specialty">Spécialité</label>
          <input type="text" id="specialty" name="specialty" value="{{ spec_q }}" placeholder="ex: Cardiologie" autocomplete="off">
          <div id="specList" class="list"></div>
        </div>
        <div>
          <button class="primary" type="submit">Rechercher</button>
          <a href="{{ url_for('index') }}"><button type="button">Réinitialiser</button></a>
        </div>
        <div>
          <a href="{{ url_for('export_csv', name=name_q, specialty=spec_q) }}"><button type="button">⬇️ Export CSV</button></a>
        </div>
      </form>
      <p class="muted">Recherche limitée à <strong>Nom</strong> et <strong>Spécialité</strong>. Auto-complétion dès 3 lettres.</p>
      <div class="stats" style="margin-top:12px">
        <div class="card" style="padding:12px"><div class="small">Résultats</div><div style="font-size:22px;font-weight:700">{{ rows|length }}</div></div>
        <div class="card" style="padding:12px"><div class="small">Total</div><div style="font-size:22px;font-weight:700">{{ total }}</div></div>
      </div>
    </div>

    <div class="card">
      {% if edit_item %}
        <h3 style="margin-top:0">✏️ Modifier</h3>
        <form class="inline" method="post" action="{{ url_for('update', item_id=edit_item['id']) }}">
          <div><label>Nom</label><input type="text" name="last_name" value="{{ edit_item['last_name'] }}" required></div>
          <div><label>Prénom</label><input type="text" name="first_name" value="{{ edit_item['first_name'] }}" required></div>
          <div><label>Adresse</label><input type="text" name="address" value="{{ edit_item['address'] }}" required></div>
          <div><label>Téléphone</label><input type="tel" name="phone" value="{{ edit_item['phone'] }}" pattern="[0-9+()\\-\\s]{6,}" required></div>
          <div class="dropdown"><label>Spécialité</label><input type="text" id="especialty" name="specialty" value="{{ edit_item['specialty'] }}" required autocomplete="off"><div id="editSpecList" class="list"></div></div>
          <div><button class="primary" type="submit">Enregistrer</button> <a href="{{ url_for('index') }}"><button type="button">Annuler</button></a></div>
        </form>
      {% else %}
        <h3 style="margin-top:0">➕ Ajouter</h3>
        <form class="inline" method="post" action="{{ url_for('create') }}">
          <div class="dropdown"><label>Nom</label><input type="text" id="clast" name="last_name" placeholder="ex: El Amrani" required autocomplete="off"><div id="cnameList" class="list"></div></div>
          <div><label>Prénom</label><input type="text" name="first_name" placeholder="ex: Youssef" required></div>
          <div><label>Adresse</label><input type="text" name="address" placeholder="ex: 12 Rue Atlas, Rabat" required></div>
          <div><label>Téléphone</label><input type="tel" name="phone" placeholder="ex: +212 6 XX XX XX XX" pattern="[0-9+()\\-\\s]{6,}" required></div>
          <div class="dropdown"><label>Spécialité</label><input type="text" id="cspec" name="specialty" placeholder="ex: Cardiologie" required autocomplete="off"><div id="cspecList" class="list"></div></div>
          <div><button class="primary" type="submit">Créer</button></div>
        </form>
        <p class="muted" style="margin-top:8px">Les doublons (Nom+Prénom+Spécialité) sont bloqués automatiquement.</p>
      {% endif %}
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h3 style="margin-top:0">🗂️ Liste</h3>
    <table><thead><tr>
      <th>#</th><th>Nom</th><th>Prénom</th><th>Adresse</th><th>Téléphone</th><th>Spécialité</th><th class="right">Actions</th>
    </tr></thead><tbody>
      {% for r in rows %}
        <tr>
          <td>{{ r['id'] }}</td>
          <td>{{ r['last_name'] }}</td>
          <td>{{ r['first_name'] }}</td>
          <td>{{ r['address'] }}</td>
          <td>{{ r['phone'] }}</td>
          <td><span class="chip">{{ r['specialty'] }}</span></td>
          <td class="right">
            <form style="display:inline" method="get" action="{{ url_for('edit', item_id=r['id']) }}"><button type="submit">Modifier</button></form>
            <form style="display:inline" method="post" action="{{ url_for('delete', item_id=r['id']) }}" onsubmit="return confirm('Supprimer cet élément ?')"><button class="danger" type="submit">Supprimer</button></form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="7" class="muted">Aucun résultat.</td></tr>
      {% endfor %}
    </tbody></table>
  </div>

  <div class="card" style="margin-top:16px">
    <h3 style="margin-top:0">📊 Nombre par spécialité (global)</h3>
    {% if per_spec %}
    <table><thead><tr><th>Spécialité</th><th>Nombre</th></tr></thead><tbody>
      {% for row in per_spec %}<tr><td>{{ row['specialty'] }}</td><td>{{ row['n'] }}</td></tr>{% endfor %}
    </tbody></table>
    {% else %}<p class="muted">Aucune donnée pour l’instant.</p>{% endif %}
  </div>
</div>

<script>
function debounce(fn,wait){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),wait)}}
function showList(id,items,input){const box=document.getElementById(id);box.innerHTML='';if(!items||!items.length){box.style.display='none';return}
items.forEach(v=>{const b=document.createElement('button');b.type='button';b.textContent=v;b.onclick=()=>{input.value=v;box.style.display='none';input.focus()};box.appendChild(b)});box.style.display='block'}
const bind=(sel,listId,endpoint)=>{const el=document.querySelector(sel);const box=document.getElementById(listId);const run=debounce(async()=>{const v=el.value.trim();if(v.length<3){box.style.display='none';return}
try{const r=await fetch(endpoint+'?q='+encodeURIComponent(v));const data=await r.json();showList(listId,data,el)}catch(e){box.style.display='none'}},200);
el.addEventListener('input',run);el.addEventListener('focus',run);el.addEventListener('blur',()=>setTimeout(()=>box.style.display='none',150))};
bind('#name','nameList','{{ url_for("api_suggest_name") }}');
bind('#specialty','specList','{{ url_for("api_suggest_specialty") }}');
bind('#clast','cnameList','{{ url_for("api_suggest_name") }}');
bind('#cspec','cspecList','{{ url_for("api_suggest_specialty") }}');
</script>
</body></html>
"""

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
