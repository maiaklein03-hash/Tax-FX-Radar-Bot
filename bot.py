
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from pypdf import PdfReader


load_dotenv()

CNV_URL = "https://www.cnv.gov.ar/sitioWeb/MarcoRegulatorio?panel=2"
BCRA_API = "https://www.bcra.gob.ar/api/endpoints/buscador-comunicaciones.php"
BCRA_BASE = "https://www.bcra.gob.ar"
BO_BASE = "https://www.boletinoficial.gob.ar"
CARPETA = Path(__file__).resolve().parent
ESTADO = CARPETA / "estado.json"
ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODO_PRUEBA = os.getenv("MODO_PRUEBA", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    )
}

# Filtro barato previo a Gemini. Solo decide qué documentos vale la pena leer.
CLAVES_FX = (
    "exterior y cambios", "mercado de cambios", "operaciones cambiarias",
    "movimientos de fondos y valores con el exterior", "camex", "refex",
    "sepex", "divisas", "deuda externa", "endeudamiento financiero",
    "títulos de deuda", "titulos de deuda", "valores negociables",
    "pagos al exterior", "cobros de exportaciones", "importaciones",
    "mep", "ccl", "contado con liquidación", "contado con liquidacion",
    "dólar", "dolar", "mlc", "mulc", "rigi", "dividendos", "utilidades",
    "canje", "precancel", "activos externos líquidos",
)

CLAVES_CNV = (
    "emisoras", "valores negociables", "oferta pública", "oferta publica",
    "obligaciones negociables", "títulos públicos", "titulos publicos",
    "colocación primaria", "colocacion primaria", "canje", "refinanciación",
    "refinanciacion", "integración", "integracion", "liquidación",
    "liquidacion", "jurisdicciones no cooperantes", "moneda extranjera",
)

CLAVES_TAX = (
    "impuesto a las ganancias", "impuesto al valor agregado",
    "bienes personales", "débitos y créditos", "debitos y creditos",
    "impuesto cedular", "obligaciones negociables", "títulos públicos",
    "titulos publicos", "jurisdicciones no cooperantes", "baja o nula",
    "doble imposición", "doble imposicion", "instrumento multilateral",
    "beneficiarios del exterior", "ley de impuesto", "tratamiento impositivo",
    "retención", "retencion", "exención", "exencion", "impuesto de sellos",
    "ingresos brutos", "transmisión gratuita", "transmision gratuita",
)

MAPA_MODELOS = """
MODELO ON CORPORATIVA
- Controles de Cambio: emisión, colocación, suscripción e integración; ingreso y
  liquidación del producido; negociación de ON; restricciones MEP/CCL que incidan
  directamente en esas operaciones; acceso al MLC para capital e intereses;
  plazos mínimos; precancelación, refinanciación y canje de ON.
- Carga Tributaria: artículo 36 y 36 bis Ley de ON; Ganancias sobre intereses y
  resultados por suscripción, tenencia, cobro o disposición; IVA; Bienes
  Personales/responsable sustituto; Débitos y Créditos aplicable a los pagos;
  jurisdicciones no cooperantes; CDI/MLI; IIBB; Sellos; transmisión gratuita de
  bienes y tasa de justicia cuando alteren el tratamiento de ON.

MODELO TÍTULOS DE DEUDA PROVINCIALES
- Cambiario: emisión e integración; ingreso y liquidación del producido;
  negociación; pagos de capital e intereses; precancelación/refinanciación;
  canje/arbitraje y MEP/CCL directamente vinculados con los títulos.
- Impositivo: Ganancias sobre intereses y ganancias de capital; IVA; Bienes
  Personales y exención de títulos públicos; Débitos y Créditos; tasa de justicia;
  jurisdicciones no cooperantes/baja tributación; CDI/MLI; IIBB; recaudación
  bancaria provincial; Sellos; transmisión gratuita de bienes.
"""


def log(mensaje):
    momento = datetime.now(ZONA_HORARIA).strftime("%d/%m/%Y %H:%M:%S")
    print(f"[{momento}] {mensaje}", flush=True)


def pedir(metodo, url, **kwargs):
    kwargs.setdefault("timeout", 50)
    kwargs.setdefault("headers", HEADERS)
    ultimo = None
    for intento in range(4):
        try:
            r = requests.request(metodo, url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as error:
            ultimo = error
            if intento < 3:
                time.sleep(2 ** intento)
    raise ultimo


def cargar_estado():
    base = {"inicializado": False, "cnv": [], "bcra": [], "bo": []}
    if not ESTADO.exists():
        return base
    try:
        base.update(json.loads(ESTADO.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(
            "estado.json está dañado; se detiene para evitar omisiones o duplicados."
        ) from error
    return base


def guardar_estado(estado):
    for fuente, limite in (("cnv", 1500), ("bcra", 3000), ("bo", 2500)):
        estado[fuente] = estado.get(fuente, [])[-limite:]
    temporal = ESTADO.with_suffix(".json.tmp")
    temporal.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporal, ESTADO)


def obtener_cnv():
    soup = BeautifulSoup(pedir("GET", CNV_URL).text, "html.parser")
    normas = []
    for fila in soup.select("table tbody tr"):
        c = fila.find_all("td")
        if len(c) < 6:
            continue
        numero = c[1].get_text(" ", strip=True)
        if not numero.startswith("RGCRGN"):
            continue
        links = [a.get("href", "") for a in fila.find_all("a", href=True)]
        descarga = next((u for u in links if "descargas" in u.lower()), "")
        infoleg = next((u for u in links if "infoleg" in u.lower()), "")
        normas.append({
            "id": numero, "fuente": "cnv", "organismo": "CNV",
            "fecha": c[0].get_text(" ", strip=True), "numero": numero,
            "seccion": c[2].get_text(" ", strip=True),
            "titulo": c[3].get_text(" ", strip=True),
            "url": infoleg or descarga, "descarga": descarga,
        })
    log(f"CNV: {len(normas)} resoluciones encontradas.")
    return normas


def obtener_bcra():
    hasta = datetime.now(ZONA_HORARIA).date()
    desde = hasta - timedelta(days=8)
    data = pedir("POST", BCRA_API, data={
        "mode": "fecha", "fecha_desde": desde.isoformat(),
        "fecha_hasta": hasta.isoformat(), "paginaabsoluta": "1",
        "tamanopagina": "200", "lang": "es",
    }, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}).json()
    normas = []
    for x in data.get("data", {}).get("registros", []):
        tipo = str(x.get("tipo", "")).upper()
        numero = str(x.get("numero_formateado", ""))
        ruta = x.get("pdf_path") or ""
        normas.append({
            "id": f"{tipo}{numero}", "fuente": "bcra", "organismo": "BCRA",
            "fecha": x.get("fecha_emision", ""),
            "numero": f"Comunicación {tipo} {numero}", "tipo": tipo,
            "seccion": "", "titulo": str(x.get("titulo", "")).strip(),
            "url": urljoin(BCRA_BASE, ruta), "descarga": urljoin(BCRA_BASE, ruta),
        })
    log(f"BCRA: {len(normas)} comunicaciones recientes encontradas.")
    return normas


def obtener_bo():
    # Revisa hoy y los cuatro días anteriores: cubre fines de semana y feriados.
    avisos = []
    vistos = set()

    def descargar_fecha(fecha):
        fecha_url = fecha.strftime("%Y%m%d")
        url = f"{BO_BASE}/seccion/primera/{fecha_url}"
        try:
            soup = BeautifulSoup(pedir("GET", url).text, "html.parser")
        except requests.RequestException:
            return fecha, []
        encontrados = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/detalleAviso/primera/" not in href:
                continue
            enlace = urljoin(url, href).split("?")[0]
            if not enlace.endswith(f"/{fecha_url}"):
                continue
            titulo = " ".join(a.get_text(" ", strip=True).split())
            if not titulo and a.parent:
                titulo = " ".join(a.parent.get_text(" ", strip=True).split())
            if not titulo:
                continue
            identificador = enlace.rstrip("/").split("/")[-2]
            encontrados.append({
                "id": identificador, "fuente": "bo", "organismo": "BO/ARCA",
                "fecha": fecha.strftime("%d/%m/%Y"), "numero": titulo,
                "seccion": "Primera Sección", "titulo": titulo,
                "url": enlace, "descarga": enlace,
            })
        return fecha, encontrados

    hoy = datetime.now(ZONA_HORARIA).date()
    fechas = [hoy - timedelta(days=i) for i in range(5)]
    with ThreadPoolExecutor(max_workers=5) as ejecutor:
        futuros = [ejecutor.submit(descargar_fecha, fecha) for fecha in fechas]
        resultados = [f.result() for f in as_completed(futuros)]

    for _, encontrados in sorted(resultados, key=lambda x: x[0], reverse=True):
        for aviso in encontrados:
            if aviso["url"] in vistos:
                continue
            vistos.add(aviso["url"])
            avisos.append(aviso)
    log(f"Boletín Oficial: {len(avisos)} avisos recientes encontrados.")
    return avisos


def candidato(norma):
    texto = f"{norma.get('seccion', '')} {norma.get('titulo', '')}".lower()
    if norma["fuente"] == "bcra":
        # Toda Comunicación A nueva se lee: títulos genéricos como "Adecuaciones"
        # pueden modificar una regla de emisión o pago. B/C conservan filtro barato.
        return norma.get("tipo") == "A" or (
            norma.get("tipo") in {"B", "C"} and any(k in texto for k in CLAVES_FX)
        )
    if norma["fuente"] == "cnv":
        # Las resoluciones generales son pocas y pueden tener títulos inespecíficos.
        return True
    es_rg_arca = (
        ("agencia de recaudación" in texto or "agencia de recaudacion" in texto)
        and "resolución general" in texto
    )
    return es_rg_arca or any(k in texto for k in CLAVES_TAX)


def extraer_pdf(contenido):
    lector = PdfReader(io.BytesIO(contenido))
    partes = []
    for pagina in lector.pages[:45]:
        partes.append(pagina.extract_text() or "")
        if sum(len(x) for x in partes) > 65000:
            break
    return "\n".join(partes)[:65000]


def obtener_texto(norma):
    r = pedir("GET", norma["descarga"] or norma["url"])
    if "pdf" in r.headers.get("Content-Type", "").lower() or r.content[:4] == b"%PDF":
        return extraer_pdf(r.content)
    soup = BeautifulSoup(r.text, "html.parser")
    for x in soup(["script", "style", "nav", "header", "footer"]):
        x.decompose()
    return "\n".join(
        linea.strip() for linea in soup.get_text("\n").splitlines() if linea.strip()
    )[:65000]


def error_gemini_reintentable(error):
    detalle = str(error).lower()
    no_reintentables = (
        "400", "404", "429", "invalid_argument", "not_found",
        "resource_exhausted", "quota",
    )
    if any(codigo in detalle for codigo in no_reintentables):
        return False
    return any(
        codigo in detalle
        for codigo in ("500", "503", "504", "unavailable", "high demand", "deadline")
    )


def evaluar_y_resumir(norma, texto):
    prompt = f"""
Sos abogado argentino especializado en debt capital markets, regulación
cambiaria y tributación de valores negociables. Compará la norma oficial con el
mapa de dos capítulos modelo de prospectos que figura abajo.

El bot no es un newsletter regulatorio. Solo existe relevancia si la norma puede
exigir modificar una afirmación del prospecto o suplemento acerca de:

1. emisión, colocación, suscripción o integración de ON/títulos;
2. negociación o liquidación de esos valores;
3. ingreso y liquidación en el MLC del producido de la emisión;
4. acceso al MLC para pagar capital o intereses, incluso precancelación,
   refinanciación o canje; o
5. consecuencias tributarias de emitir, suscribir, tener, cobrar, vender,
   transferir o ejecutar las ON/títulos.

Aplicá estrictamente estas reglas:
- Debe existir un nexo directo con al menos uno de esos cinco puntos. Que una
  norma sea cambiaria, tributaria, financiera o interesante para el mercado no
  alcanza.
- La mera mención, remisión o cita del punto 3.5 del T.O. de Exterior y Cambios
  no alcanza. Identificá qué inciso o condición cambia y cómo incide en la
  emisión, negociación, liquidación del producido o servicio de deuda.
- Si regula exclusivamente RIGI, VPU, proyectos adheridos o destinos RIGI,
  respondé NO_RELEVANTE. No presumas que la operación está bajo RIGI. Esas normas
  se incorporarán únicamente cuando exista una operación RIGI identificada.
- Son NO_RELEVANTES las normas sobre bandas o régimen macroeconómico, comercio
  exterior, dividendos, personas humanas, regulación bancaria prudencial o
  sectores especiales si no cambian directamente uno de los cinco puntos.
- En Tax, excluí reformas generales que no alteren el tratamiento del emisor,
  inversor, tenedor, beneficiario o pagos específicamente vinculados a ON/títulos.
- Una incidencia contextual, eventual, remota o meramente informativa es
  NO_RELEVANTE.

Si no supera esta prueba, respondé únicamente: NO_RELEVANTE

Si la supera, redactá un mensaje de hasta 1.350 caracteres con esta forma:

ACTUALIZACIÓN TAX & CAMBIARIA — [ORGANISMO Y NORMA]
[Título o tema en una línea]

• Cambio: qué modificó concretamente.
• Prospecto alcanzado: ON / Provincial / Ambos.
• Apartado a revisar: nombre preciso del apartado del modelo.
• Texto afectado: qué afirmación, requisito, plazo, tasa, exención o referencia
  concreta podría necesitar actualización.
• Vigencia: fecha y transición; si no surge, decirlo.
• Acción sugerida: verificación concreta y breve, sin redactar texto legal nuevo.

Reglas: español argentino; tono profesional; sin saludo; sin enlaces; distinguir
consulta pública de norma definitiva; no afirmar que el capítulo necesariamente
debe cambiar si solo requiere revisión; preservar cifras y fechas esenciales.

{MAPA_MODELOS}

NORMA A EVALUAR
Organismo: {norma['organismo']}
Número: {norma['numero']}
Fecha: {norma['fecha']}
Sección/asunto: {norma.get('seccion', '')} — {norma['titulo']}

TEXTO OFICIAL
{texto}
"""
    cliente = genai.Client(api_key=GEMINI_API_KEY)
    ultimo = None
    for intento in range(1, 3):
        try:
            r = cliente.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            if r.text:
                return r.text.strip()
            raise RuntimeError("Gemini no devolvió texto.")
        except Exception as error:
            ultimo = error
            log(f"Gemini falló (intento {intento}/2): {error}")
            if intento >= 2 or not error_gemini_reintentable(error):
                break
            time.sleep(12)
    raise RuntimeError(f"Gemini no respondió: {ultimo}")


def enviar(texto):
    # No se reintenta automáticamente un POST: si Telegram lo aceptó pero se
    # perdió la respuesta, repetirlo podría duplicar el mensaje.
    respuesta = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": texto,
            "disable_web_page_preview": True,
        },
        timeout=50,
    )
    respuesta.raise_for_status()
    resultado = respuesta.json()
    if not resultado.get("ok"):
        raise RuntimeError(resultado)


def analizar(norma):
    log(f"Analizando {norma['organismo']} {norma['id']}: {norma['titulo']}")
    texto = obtener_texto(norma)
    if len(texto.strip()) < 100:
        raise RuntimeError("No se obtuvo texto oficial suficiente.")
    resultado = evaluar_y_resumir(norma, texto)
    if resultado.strip().upper().startswith("NO_RELEVANTE"):
        log(f"{norma['id']}: sin impacto material en los modelos.")
        return False
    mensaje = f"{resultado}\n\nNorma oficial: {norma['url']}"
    enviar(mensaje)
    return True


def ejecutar():
    if not all((TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY)):
        raise RuntimeError("Falta configurar TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID o GEMINI_API_KEY.")

    estado = cargar_estado()
    por_fuente = {"cnv": obtener_cnv(), "bcra": obtener_bcra(), "bo": obtener_bo()}

    if MODO_PRUEBA:
        # Prueba la primera candidata de cada fuente; no toca el historial.
        candidatas = []
        for fuente in ("bcra", "cnv", "bo"):
            candidatas.extend([n for n in por_fuente[fuente] if candidato(n)][:1])
        enviadas = sum(analizar(n) for n in candidatas)
        enviar(
            "PRUEBA COMPLETADA\n"
            f"Documentos evaluados: {len(candidatas)}. Alertas relevantes: {enviadas}."
        )
        log("Modo prueba terminado.")
        return

    if not estado.get("inicializado"):
        for fuente, normas in por_fuente.items():
            estado[fuente] = [n["id"] for n in normas]
        estado["inicializado"] = True
        guardar_estado(estado)
        log("Foto inicial guardada. No se enviaron normas anteriores.")
        return

    nuevas = []
    for fuente, normas in por_fuente.items():
        conocidas = set(estado.get(fuente, []))
        nuevas.extend(n for n in reversed(normas) if n["id"] not in conocidas and candidato(n))

    enviadas = 0
    fallidas = set()
    for norma in nuevas:
        try:
            enviadas += int(analizar(norma))
            # Persistir cada resultado evita repetir alertas ya enviadas si una
            # norma posterior falla o el runner se interrumpe.
            historial = estado.setdefault(norma["fuente"], [])
            if norma["id"] not in historial:
                historial.append(norma["id"])
                guardar_estado(estado)
        except Exception as error:
            # No se marca como revisada: se volverá a intentar en la próxima corrida.
            fallidas.add(norma["id"])
            log(f"ERROR {norma['id']}: {error}")
            continue

    # Guarda todo lo que la fuente devolvió. Las normas que fallaron se excluyen
    # para permitir el reintento.
    for fuente, normas in por_fuente.items():
        existentes = set(estado.get(fuente, []))
        estado[fuente].extend(
            n["id"] for n in normas if n["id"] not in existentes and n["id"] not in fallidas
        )
    guardar_estado(estado)
    log(f"Ejecución terminada. Candidatas nuevas: {len(nuevas)}. Alertas: {enviadas}.")


if __name__ == "__main__":
    ejecutar()
