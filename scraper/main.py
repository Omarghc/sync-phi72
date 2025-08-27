from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import json, os, re, unicodedata
from datetime import datetime, timedelta, timezone

# --- FCM V1 ---
import requests
from google.oauth2 import service_account
import google.auth.transport.requests

# === Proyecto / Topics ===
PROJECT_ID = "bancard-a52ba"            # <-- tu proyecto
TOPIC_GLOBAL = "resultados_loteria"     # usuarios sin favoritas

# === TZ RD (sin DST) ===
TZ_RD = timezone(timedelta(hours=-4), name="America/Santo_Domingo")

MESES = {
    'enero':'01','febrero':'02','marzo':'03','abril':'04','mayo':'05','junio':'06',
    'julio':'07','agosto':'08','septiembre':'09','setiembre':'09','octubre':'10','noviembre':'11','diciembre':'12'
}

# ---------- Utilidades ----------
def normaliza_fecha(fecha: str) -> str:
    """A yyyy-MM-dd cuando es posible."""
    if not fecha:
        return fecha
    fecha = fecha.strip()

    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", fecha)
    if m:  # dd-MM-yyyy -> yyyy-MM-dd
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    m = re.match(r"^(\d{1,2})\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ]+)$", fecha)
    if m:  # "15 julio" -> yyyy-07-15 (año actual)
        hoy = datetime.now(TZ_RD)
        dia = int(m.group(1))
        mes = MESES.get(m.group(2).lower(), "01")
        return f"{hoy.year}-{mes}-{dia:02d}"

    return fecha

def sanitizar_logo(url: str) -> str:
    if not url:
        return url
    return re.sub(r'\?.*$', '', url)

def _plain_lower(s: str) -> str:
    s = unicodedata.normalize('NFD', s or '').encode('ascii','ignore').decode('utf-8')
    s = re.sub(r'\s+', ' ', s.strip()).lower()
    return s

CANON_MAP = {
    "leidsa noche": "Quiniela Leidsa",
    "loteka noche": "Quiniela Loteka",
    "loteria real tarde": "Quiniela Real",
    "loteria nacional noche": "Lotería Nacional",
    "loteria nacional tarde (gana mas)": "Gana Más",
    "loteria florida noche": "Florida Noche",
    "loteria florida tarde": "Florida Día",
    "la suerte noche": "La Suerte 18:00",
    "la suerte medio dia": "La Suerte 12:30",
    "king lottery noche": "King Lottery 7:30",
    "king lottery medio dia": "King Lottery 12:30",
    "king lottery tarde": "King Lottery 7:30",
    "la primera tarde": "La Primera Día",
    "la primera noche": "Primera Noche",
    "new york noche": "New York Noche",
    "new york tarde": "New York Tarde",
    "anguila mañana 8am": "Anguila Mañana",
    "anguila mañana 11am": "Anguila Mañana",
    "anguila medio dia 12pm": "Anguila Medio Día",
    "anguila tarde 1:00pm": "Anguila Tarde",
    "anguila tarde 2pm": "Anguila Tarde",
    "anguila tarde 3pm": "Anguila Tarde",
    "anguila tarde 4pm": "Anguila Tarde",
    "anguila tarde 5pm": "Anguila Tarde",
    "anguila tarde 6:00pm": "Anguila Tarde",
    "anguila noche 7pm": "Anguila Noche",
    "anguila noche 8pm": "Anguila Noche",
    "anguila noche 9:00pm": "Anguila Noche",
    "anguila noche 10pm": "Anguila Noche",
}

def canonicaliza_loteria(nombre: str) -> str:
    k = re.sub(r'^(loteria|lottery)\s+', '', _plain_lower(nombre))
    return CANON_MAP.get(k, nombre)

def topic_seguro(nombre: str) -> str:
    s = unicodedata.normalize('NFD', nombre or '').encode('ascii', 'ignore').decode('utf-8')
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return 'loteria_' + s

def nums_key(numeros)->str:
    arr = []
    for x in (numeros or []):
        m = re.findall(r"\d+", str(x))
        if m: arr.append(m[0])
    arr = sorted(arr, key=lambda x:int(x))
    return "-".join(arr)

# --- clave única para dedupe (SIN hora) ---
def make_dedupe_key(loteria: str, numeros: list, fecha: str) -> str:
    lot = canonicaliza_loteria(loteria or "")
    nums = ",".join(numeros or [])
    base = f"{lot}|{nums}|{fecha}"
    return re.sub(r'\s+', ' ', base).strip()

# ---------- Parseo fecha/hora a datetime (TZ RD) ----------
def parse_dt(item) -> datetime|None:
    raw_fecha = (item.get('fecha') or item.get('fecha_original') or '').strip()
    raw_fecha = normaliza_fecha(raw_fecha)
    raw_hora  = (item.get('hora') or '').strip()

    # yyyy-MM-dd (+ hora AM/PM)
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', raw_fecha)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = 12, 0
        if raw_hora:
            h = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$', raw_hora.replace(' ', ''))
            if h:
                hh = int(h.group(1)); mm = int(h.group(2))
                ampm = h.group(3).upper()
                if ampm == 'PM' and hh != 12: hh += 12
                if ampm == 'AM' and hh == 12: hh = 0
        return datetime(y, mo, d, hh, mm, tzinfo=TZ_RD)

    # dd-MM-yyyy HH:mm
    m = re.match(r'^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})$', raw_fecha)
    if m:
        d, mo, y, hh, mm = map(int, m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=TZ_RD)

    # "dd mes" (+hora) -> año actual
    m = re.match(r'^(\d{1,2})\s+([a-záéíóúñ]+)$', raw_fecha.lower())
    if m:
        d = int(m.group(1)); mes_txt = m.group(2)
        mo = int(MESES.get(mes_txt, '00'))
        if mo == 0: return None
        now = datetime.now(TZ_RD)
        hh, mm = 12, 0
        if raw_hora:
            h = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$', raw_hora.replace(' ', ''))
            if h:
                hh = int(h.group(1)); mm = int(h.group(2))
                ampm = h.group(3).upper()
                if ampm == 'PM' and hh != 12: hh += 12
                if ampm == 'AM' and hh == 12: hh = 0
        return datetime(now.year, mo, d, hh, mm, tzinfo=TZ_RD)
    return None

def is_today(dt: datetime) -> bool:
    now = datetime.now(TZ_RD)
    return (dt.year, dt.month, dt.day) == (now.year, now.month, now.day)

# ---------- Scrapers ----------
def scrapear_loterias_dominicanas():
    resultados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://loteriasdominicanas.com/pagina/ultimos-resultados", timeout=60000)
            page.wait_for_selector("div.game-info.p-2", timeout=20000)
            html = page.content()
            soup = BeautifulSoup(html, 'html.parser')
            juegos = soup.select("div.game-info.p-2")
            for juego in juegos:
                try:
                    fecha_tag = juego.select_one(".session-date")
                    nombre_tag = juego.select_one(".game-title span")
                    numeros_tag = juego.find_next_sibling("div", class_="game-scores")
                    logo_div = juego.select_one("div.game-logo")
                    img_url = ""
                    if logo_div:
                        img_tag = logo_div.find("img")
                        if img_tag:
                            img_url = img_tag.get("src", "") or img_tag.get("data-src", "")
                    if img_url.startswith("/"):
                        img_url = "https://loteriasdominicanas.com" + img_url
                    img_url = sanitizar_logo(img_url)

                    if not (fecha_tag and nombre_tag and numeros_tag):
                        continue
                    fecha = fecha_tag.get_text(strip=True)
                    fecha_normalizada = normaliza_fecha(fecha)
                    nombre = nombre_tag.get_text(strip=True)
                    numeros = [n.get_text(strip=True) for n in numeros_tag.select("span.score")]

                    resultados.append({
                        'fuente': 'loteriasdominicanas.com',
                        'loteria': nombre,
                        'img': img_url,
                        'numeros': numeros,
                        'fecha_original': fecha,
                        'fecha': fecha_normalizada,
                        'hora': None,  # esta fuente no trae hora
                        'hora_scrapeo': datetime.now(TZ_RD).strftime('%Y-%m-%d %H:%M:%S')
                    })
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        print(f"❌ Error loteriasdominicanas.com: {e}")
    return resultados

def scrapear_tusnumerosrd():
    resultados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://www.tusnumerosrd.com/resultados.php", timeout=60000)
            page.wait_for_timeout(6000)
            html = page.content()
            soup = BeautifulSoup(html, 'html.parser')
            filas = soup.select("tr")
            for fila in filas:
                try:
                    nombre_tag = fila.select_one("h6.mb-0")
                    if not nombre_tag:
                        continue
                    nombre = nombre_tag.get_text(strip=True)
                    img_tag = fila.select_one("img")
                    img_url = img_tag["src"] if img_tag and "src" in img_tag.attrs else ""
                    if img_url and img_url.startswith('/'):
                        img_url = "https://www.tusnumerosrd.com" + img_url
                    img_url = sanitizar_logo(img_url)
                    numeros = [n.get_text(strip=True) for n in fila.select("div.badge.badge-primary.badge-dot")]
                    fecha_tag = fila.select_one("span.table-inner-text")
                    fecha = fecha_tag.get_text(strip=True) if fecha_tag else ""
                    fecha_normalizada = normaliza_fecha(fecha)
                    celdas = fila.find_all("td", class_="text-center")
                    hora = celdas[-1].get_text(strip=True) if celdas else None

                    if nombre and numeros:
                        resultados.append({
                            'fuente': 'tusnumerosrd.com',
                            'loteria': nombre,
                            'img': img_url,
                            'numeros': numeros,
                            'fecha_original': fecha,
                            'fecha': fecha_normalizada,
                            'hora': hora,
                            'hora_scrapeo': datetime.now(TZ_RD).strftime('%Y-%m-%d %H:%M:%S')
                        })
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        print(f"❌ Error tusnumerosrd.com: {e}")
    return resultados

# ---------- Persistencia ----------
def cargar_historico(path="resultados_combinados.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict) and "resultados" in data:
                    return data["resultados"]
                return data if isinstance(data, list) else []
            except Exception:
                return []
    return []

def _clave(r):
    return (r.get('loteria',''), tuple(r.get('numeros') or []), r.get('fecha',''), r.get('hora'))

def evitar_duplicados(resultados_viejos, nuevos):
    existentes = set(_clave(r) for r in resultados_viejos)
    no_duplicados = [r for r in nuevos if _clave(r) not in existentes]
    return resultados_viejos + no_duplicados

def delta_nuevos(historico, nuevos):
    existentes = set(_clave(r) for r in historico)
    return [r for r in nuevos if _clave(r) not in existentes]

# --- dedupe entre fuentes (misma lotería/fecha/números) ---
def _grupo_clave(r):
    lot_can = canonicaliza_loteria(r.get('loteria', '') or '')
    fecha = r.get('fecha') or ''
    numeros = tuple(r.get('numeros') or [])
    return (lot_can, fecha, numeros)

def compactar_delta(delta):
    grupos = {}
    for r in delta:
        k = _grupo_clave(r)
        prev = grupos.get(k)
        if not prev:
            grupos[k] = r
            continue
        h_prev = (prev.get('hora') or '').strip()
        h_new  = (r.get('hora') or '').strip()
        if h_prev and not h_new:
            pass
        elif (not h_prev) and h_new:
            grupos[k] = r
        else:
            if (r.get('hora_scrapeo') or '') > (prev.get('hora_scrapeo') or ''):
                grupos[k] = r
    return list(grupos.values())

# ---------- FCM ----------
def _get_fcm_credentials():
    """Lee JSON completo desde FCM_SERVICE_ACCOUNT_JSON o ruta en GOOGLE_APPLICATION_CREDENTIALS."""
    SCOPES = ['https://www.googleapis.com/auth/firebase.messaging']
    env_json = os.getenv("FCM_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            info = json.loads(env_json)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            print(f"⚠️ SA en env inválida: {e}")
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if sa_path and os.path.isfile(sa_path):
        try:
            return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        except Exception as e:
            print(f"⚠️ SA por archivo inválida: {e}")
    print("❌ SA: no encontrada")
    return None

def enviar_fcm_v1_data(topic: str, data: dict, collapse_key: str, ttl_seconds: int = 900):
    """DATA-ONLY (sin 'notification') para que el cliente pueda filtrar; colapsa en tránsito."""
    creds = _get_fcm_credentials()
    if not creds:
        print("⚠️ FCM omitido: credenciales no disponibles.")
        return
    req = google.auth.transport.requests.Request()
    creds.refresh(req)
    token = creds.token

    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    message = {
        "message": {
            "topic": topic,
            "data": data,
            "android": {
                "ttl": f"{ttl_seconds}s",
                "priority": "HIGH",
                "collapse_key": collapse_key,
            }
        }
    }
    r = requests.post(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, json=message, timeout=15)
    if r.status_code >= 300:
        print(f"⚠️ Error FCM {r.status_code}: {r.text}")
    else:
        print(f"✅ FCM enviado a /topics/{topic}")

# ---------- Cache de envíos (idempotencia) ----------
SENT_CACHE = "sent_cache.json"

def load_sent_cache():
    try:
        with open(SENT_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_sent_cache(cache: dict):
    now = datetime.now(TZ_RD).timestamp()
    # purga > 3 días
    cache = {k:v for k,v in cache.items() if now - float(v) < 3*24*3600}
    with open(SENT_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

# ---------- MAIN ----------
def main():
    print("🔍 Buscando en loteriasdominicanas.com...")
    resultados_ld = scrapear_loterias_dominicanas()
    print(f"✅ {len(resultados_ld)} resultados en loteriasdominicanas.com")

    print("🔍 Buscando en tusnumerosrd.com...")
    resultados_tn = scrapear_tusnumerosrd()
    print(f"✅ {len(resultados_tn)} resultados en tusnumerosrd.com")

    nuevos = resultados_ld + resultados_tn

    # 1) SOLO HOY (RD)
    solo_hoy = []
    for r in nuevos:
        dt = parse_dt(r)
        if dt and is_today(dt):
            r['_dt'] = dt
            solo_hoy.append(r)

    if not solo_hoy:
        print("⚠️ No hay resultados de HOY para enviar.")
    # 2) Persistencia del archivo público (guardamos lo de hoy sobre histórico)
    historico = cargar_historico()
    delta = delta_nuevos(historico, solo_hoy)
    delta = compactar_delta(delta)

    resultados_actualizados = evitar_duplicados(historico, solo_hoy)
    with open("resultados_combinados.json", "w", encoding="utf-8") as f:
        json.dump({
            "generado": datetime.now(TZ_RD).isoformat(),
            "resultados": resultados_actualizados
        }, f, indent=2, ensure_ascii=False)
    print(f"📦 Guardados {len(resultados_actualizados)} en resultados_combinados.json")
    print(f"➕ Nuevos HOY a enviar: {len(delta)}")

    if not delta:
        return

    # 3) Idempotencia entre corridas
    sent_cache = load_sent_cache()

    # 4) Envío por lotería canónica (toma el más reciente por _dt)
    por_loteria = {}
    for r in delta:
        lot_can = canonicaliza_loteria(r['loteria'])
        por_loteria.setdefault(lot_can, []).append(r)

    for lot, items in por_loteria.items():
        items.sort(key=lambda x: x.get('_dt') or datetime.min.replace(tzinfo=TZ_RD))
        last = items[-1]
        fecha_txt = last.get('fecha') or ""
        hora_txt  = last.get('hora') or ""
        numeros   = last.get('numeros') or []
        nums_txt  = "·".join([str(x).zfill(2) for x in numeros])

        dedupe_id = f"{topic_seguro(lot)}|{nums_key(numeros)}|{fecha_txt}"
        if dedupe_id in sent_cache:
            print(f"↩️ Ya enviado (cache): {dedupe_id}")
            continue

        payload = {
            "type": "resultado",
            "loteria": lot,
            "fecha": fecha_txt,
            "hora": hora_txt,
            "numeros": nums_txt,
            "fuente": last.get('fuente', ''),
        }

        topic_especifico = topic_seguro(lot)
        collapse = f"{topic_especifico}_{fecha_txt}"

        # a) tópico específico
        enviar_fcm_v1_data(topic_especifico, payload, collapse_key=collapse, ttl_seconds=900)
        # b) tópico global
        enviar_fcm_v1_data(TOPIC_GLOBAL, payload, collapse_key=collapse, ttl_seconds=900)

        sent_cache[dedupe_id] = datetime.now(TZ_RD).timestamp()

    save_sent_cache(sent_cache)

if __name__ == "__main__":
    main()
