from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import json
from datetime import datetime
import os
import re
import unicodedata

# --- FCM V1 DEPENDENCIAS ---
import requests
from google.oauth2 import service_account
import google.auth.transport.requests

# === FALLBACK LOCAL PARA LA CREDENCIAL (no depende del env var) ===
LOCAL_FCM_KEY = r"C:\Users\jansel.sanchez\Secrets\bancard-a52ba-afdeebce358b.json"
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.isfile(LOCAL_FCM_KEY):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = LOCAL_FCM_KEY

# === CONFIGURA AQUÍ TU JSON/PROYECTO ===
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'bancard-a52ba-afdeebce358b.json')
PROJECT_ID = "bancard-a52ba"
TOPIC_GLOBAL = "resultados_loteria"
ANDROID_CHANNEL_ID = "resultados_loteria_high"  # Debe existir en tu app (Manifest)

MESES = {
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04', 'mayo': '05', 'junio': '06',
    'julio': '07', 'agosto': '08', 'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12'
}

def normaliza_fecha(fecha):
    # 15-07-2025  -> 2025-07-15
    match = re.match(r"(\d{2})-(\d{2})-(\d{4})", fecha)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    # 15 julio -> YYYY-07-15 (asume año actual)
    match = re.match(r"(\d{2})\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ]+)", fecha)
    if match:
        hoy = datetime.now()
        dia = match.group(1)
        mes = MESES.get(match.group(2).lower(), "01")
        return f"{hoy.year}-{mes}-{dia}"
    return fecha

def sanitizar_logo(url: str) -> str:
    if not url:
        return url
    return re.sub(r'\?.*$', '', url)

def topic_seguro(nombre: str) -> str:
    # sin acentos, minúsculas y no alfanum → _
    s = unicodedata.normalize('NFD', nombre).encode('ascii', 'ignore').decode('utf-8')
    s = s.lower()
    s = re.sub(r'[^a-z0-9]', '_', s)
    return 'loteria_' + s

# ---------- Canonicalización ----------
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

# --- clave única para deduplicación --- (SIN hora, para colapsar si 2 fuentes difieren en hora)
def make_dedupe_key(loteria: str, numeros: list, fecha: str, hora: str|None) -> str:
    lot = canonicaliza_loteria(loteria or "")
    nums = ",".join(numeros or [])
    base = f"{lot}|{nums}|{fecha}"          # <<<< sin hora
    base = re.sub(r'\s+', ' ', base).strip()
    return base

# --------------------------------------------------------------------------------------

def scrapear_loterias_dominicanas():
    resultados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto("https://loteriasdominicanas.com/pagina/ultimos-resultados", timeout=60000)
            page.wait_for_selector("div.game-info.p-2", timeout=20000)
            html = page.content()
            with open("debug_loterias.html", "w", encoding="utf-8") as f:
                f.write(html)
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
                        print("[LoteriasDom] ⛔ Falta dato, se omite.")
                        continue
                    fecha = fecha_tag.get_text(strip=True)
                    fecha_normalizada = normaliza_fecha(fecha)
                    nombre = nombre_tag.get_text(strip=True)
                    numeros = [n.get_text(strip=True) for n in numeros_tag.select("span.score")]
                    print(f"[LoteriasDom] Fecha: {fecha_normalizada} | Lotería: {nombre} | Números: {numeros}")
                    resultados.append({
                        'fuente': 'loteriasdominicanas.com',
                        'loteria': nombre,
                        'img': img_url,
                        'numeros': numeros,
                        'fecha_original': fecha,
                        'fecha': fecha_normalizada,
                        'hora': None,  # esta fuente no trae hora
                        'hora_scrapeo': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                except Exception as e:
                    print(f"[LoteriasDom] Error juego: {e}")
                    continue
            browser.close()
    except Exception as e:
        print(f"❌ Error al scrapear loteriasdominicanas.com: {e}")
    return resultados

def scrapear_tusnumerosrd():
    resultados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto("https://www.tusnumerosrd.com/resultados.php", timeout=60000)
            page.wait_for_timeout(9000)
            html = page.content()
            with open("debug_tusnumerosrd.html", "w", encoding="utf-8") as f:
                f.write(html)
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
                    fecha = fecha_tag.get_text(strip=True) if fecha_tag else "Fecha no encontrada"
                    fecha_normalizada = normaliza_fecha(fecha)
                    celdas = fila.find_all("td", class_="text-center")
                    hora = celdas[-1].get_text(strip=True) if celdas else None
                    print(f"[TusNumerosRD] Fecha: {fecha_normalizada} | Lotería: {nombre} | Números: {numeros}")
                    if nombre and numeros:
                        resultados.append({
                            'fuente': 'tusnumerosrd.com',
                            'loteria': nombre,
                            'img': img_url,
                            'numeros': numeros,
                            'fecha_original': fecha,
                            'fecha': fecha_normalizada,
                            'hora': hora,
                            'hora_scrapeo': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                except Exception as e:
                    print(f"[TusNumerosRD] Error fila: {e}")
                    continue
            browser.close()
    except Exception as e:
        print(f"❌ Error al scrapear tusnumerosrd.com: {e}")
    return resultados

def cargar_historico(path="resultados_combinados.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []

# ======== CLAVE con HORA para deduplicar y contar ========
def _clave(r):
    return (r['loteria'], tuple(r['numeros']), r['fecha'], r.get('hora'))

def evitar_duplicados(resultados_viejos, nuevos):
    existentes = set(_clave(r) for r in resultados_viejos)
    no_duplicados = []
    for r in nuevos:
        if _clave(r) not in existentes:
            no_duplicados.append(r)
    return resultados_viejos + no_duplicados

def delta_nuevos(historico, nuevos):
    existentes = set(_clave(r) for r in historico)
    return [r for r in nuevos if _clave(r) not in existentes]

def _contar_nuevos_exclusivos(historico, nuevos):
    existentes = set(_clave(r) for r in historico)
    return sum(1 for r in nuevos if _clave(r) not in existentes)

# ---------- Credenciales FCM ----------
def _get_fcm_credentials():
    SCOPES = ['https://www.googleapis.com/auth/firebase.messaging']
    env_json = os.getenv("FCM_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            info = json.loads(env_json)
            if info.get("project_id") and info.get("project_id") != PROJECT_ID:
                print(f"⚠️ SA project_id {info.get('project_id')} != {PROJECT_ID}")
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            print(f"⚠️ SA en env inválida: {e}")
    if SERVICE_ACCOUNT_FILE and os.path.isfile(SERVICE_ACCOUNT_FILE):
        try:
            return service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        except Exception as e:
            print(f"⚠️ SA por archivo inválida: {e}")
    return None

# ========== FUNCION FCM V1 ==========
def enviar_fcm_v1(title, body, topic="resultados_loteria", data=None):
    credentials = _get_fcm_credentials()
    if not credentials:
        print("⚠️ FCM omitido: credenciales no disponibles.")
        return

    try:
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        access_token = credentials.token

        # usa dedupe_key como tag si viene en data (sin hora)
        dedupe_tag = None
        if data and isinstance(data, dict):
            dedupe_tag = data.get("dedupe_key")

        url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
        message = {
            "message": {
                "topic": topic,
                "notification": {
                    "title": title,
                    "body": body
                },
                "android": {
                    "priority": "HIGH",
                    "notification": {
                        "channel_id": ANDROID_CHANNEL_ID,
                        "tag": dedupe_tag or topic
                    }
                },
                "data": data or {}
            }
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        response = requests.post(url, headers=headers, json=message, timeout=15)
        if response.ok:
            print(f"✅ Notificación enviada a /topics/{topic}")
        else:
            print(f"⚠️ Error enviando notificación FCM v1: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"⚠️ FCM: excepción no controlada: {e}")

# ======== COMPACTAR DELTA (evitar duplicados entre fuentes) ========
def _grupo_clave(r):
    lot_can = canonicaliza_loteria(r.get('loteria', '') or '')
    fecha = r.get('fecha') or ''
    numeros = tuple(r.get('numeros') or [])
    return (lot_can, fecha, numeros)

def compactar_delta(delta):
    """
    Agrupa por (lotería CANÓNICA, fecha, números) e intenta elegir
    el mejor registro (prefiere el que tenga hora; si no, el más reciente por hora_scrapeo).
    """
    grupos = {}
    for r in delta:
        k = _grupo_clave(r)
        prev = grupos.get(k)
        if not prev:
            grupos[k] = r
            continue
        # Preferir el que tiene hora
        h_prev = (prev.get('hora') or '').strip()
        h_new = (r.get('hora') or '').strip()
        if h_prev and not h_new:
            pass  # deja prev
        elif (not h_prev) and h_new:
            grupos[k] = r
        else:
            # si ambos tienen (o ninguno), elegir por hora_scrapeo más reciente
            if (r.get('hora_scrapeo') or '') > (prev.get('hora_scrapeo') or ''):
                grupos[k] = r
    return list(grupos.values())

# ========== MAIN ==========
def main():
    print("🔍 Buscando en loteriasdominicanas.com...")
    resultados_ld = scrapear_loterias_dominicanas()
    print(f"✅ {len(resultados_ld)} resultados encontrados en loteriasdominicanas.com")

    print("🔍 Buscando en tusnumerosrd.com...")
    resultados_tn = scrapear_tusnumerosrd()
    print(f"✅ {len(resultados_tn)} resultados encontrados en tusnumerosrd.com")

    nuevos_resultados = resultados_ld + resultados_tn

    if nuevos_resultados:
        historico = cargar_historico()
        nuevos_agregados = _contar_nuevos_exclusivos(historico, nuevos_resultados)
        delta = delta_nuevos(historico, nuevos_resultados)

        # === SOLO ESTA LÍNEA ES LA CLAVE: compacta el delta para no duplicar por "hora" ===
        delta = compactar_delta(delta)

        resultados_actualizados = evitar_duplicados(historico, nuevos_resultados)

        with open("resultados_combinados.json", "w", encoding="utf-8") as f:
            json.dump(resultados_actualizados, f, indent=4, ensure_ascii=False)

        print(f"📦 Se guardaron {len(resultados_actualizados)} resultados en 'resultados_combinados.json'")
        print(f"➕ Nuevos resultados agregados: {len(delta)}")  # reflejamos los realmente nuevos a enviar

        if delta:
            # Agrupa por lotería CANÓNICA
            por_loteria = {}
            for r in delta:
                lot_can = canonicaliza_loteria(r['loteria'])
                por_loteria.setdefault(lot_can, []).append(r)

            # Por cada lotería: envía a específico y global con el mismo dedupe_key (SIN hora)
            for lot, items in por_loteria.items():
                last = items[-1]  # más reciente de esta corrida
                numeros = last.get('numeros') or []
                numeros_txt = " ".join(numeros)
                fecha_txt = last.get('fecha') or ""
                hora_txt = last.get('hora') or ""
                top = topic_seguro(lot)

                dedupe_key = make_dedupe_key(lot, numeros, fecha_txt, hora_txt)

                titulo = f"Resultados de {lot}"
                cuerpo = f"{numeros_txt} • {fecha_txt}" + (f" · {hora_txt}" if hora_txt else "")

                payload = {
                    "type": "resultado",
                    "loteria": lot,
                    "fecha": fecha_txt,
                    "hora": hora_txt,
                    "numeros": ",".join(numeros),
                    "fuente": last.get('fuente', ''),
                    "dedupe_key": dedupe_key,
                }

                # a) tópico específico
                enviar_fcm_v1(titulo, cuerpo, top, data=payload)

                # b) tópico global (para usuarios sin favoritas)
                enviar_fcm_v1(titulo, cuerpo, TOPIC_GLOBAL, data=payload)

    else:
        print("⚠️ No se pudo extraer ningún resultado de ninguna fuente.")

if __name__ == "__main__":
    main()
