"""
Prueba de ElevenLabs como proveedor de voz, SIN RED Y SIN GASTAR NADA.

Todo lo que saldria a la API se sustituye por respuestas fabricadas aqui: el
motor nunca llega a `requests` de verdad, la clave es de mentira y la sintesis
del pipeline corre en modo simulado. Lo que se prueba:

  [1]  el formato de audio va en la URL (output_format), no en el cuerpo
  [2]  la respuesta se valida por lo que LLEGA: PCM, WAV, MP3 (nunca leido como
       PCM), basura, longitud impar, frecuencia que no casa con las marcas
  [3]  si la cuenta rechaza el formato se prueba otro, y se convierte al interno
  [4]  reintentos (429/5xx), clave mala, voz que no existe, limite de caracteres
  [5]  alineacion por caracter -> palabras, y tomas por secciones con contexto
  [6]  el catalogo de voces de la cuenta (/v2/voices, paginado)
  [7]  la configuracion: sin voz por defecto, formato, frecuencia, presets
  [8]  voces y modelos incompatibles: se AVISA, no se cambia el proveedor
  [9]  proyectos antiguos: siguen en Cartesia, con su firma y su frecuencia
  [10] el pipeline simulado con ElevenLabs: frecuencia, meta y regrabado
  [11] costes separados por proveedor y modelo
  [12] modo light (encargo), voz descrita y ajustes de los videos nuevos

    python pasos/prueba_elevenlabs.py
"""
import base64
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import wave

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AQUI))
sys.path.insert(0, AQUI)

# NADA DE LO DE VERDAD: carpeta de secretos, ajustes, coste y tarifas de esta
# prueba, antes de importar nada que lea esas rutas al cargarse.
BASE = tempfile.mkdtemp(prefix="estudio_elevenlabs_")
os.environ["ESTUDIO_SIMULAR"] = "1"
os.environ["ESTUDIO_SECRETOS"] = os.path.join(BASE, "secretos")
os.environ["ESTUDIO_AJUSTES"] = os.path.join(BASE, "ajustes.json")
os.environ["ESTUDIO_COSTE_GLOBAL"] = os.path.join(BASE, "coste_global.jsonl")
os.environ["ESTUDIO_ESTADISTICAS"] = os.path.join(BASE, "estadisticas.json")
os.environ.pop("CARTESIA_API_KEY", None)
CLAVE_FALSA = "clave-falsa-de-prueba-0123456789"
os.environ["ELEVENLABS_API_KEY"] = CLAVE_FALSA
os.makedirs(os.environ["ESTUDIO_SECRETOS"], exist_ok=True)
_TARIFAS_ORIGINAL = os.path.join(os.path.dirname(AQUI), "tarifas.json")
os.environ["ESTUDIO_TARIFAS"] = os.path.join(BASE, "tarifas.json")
shutil.copy2(_TARIFAS_ORIGINAL, os.environ["ESTUDIO_TARIFAS"])

import requests  # noqa: E402

from nucleo.estado import Estado  # noqa: E402
from nucleo.proyecto import Proyecto  # noqa: E402
from nucleo import coste  # noqa: E402

import ajustes  # noqa: E402
import comun  # noqa: E402
import p4_voz  # noqa: E402
import presets_light  # noqa: E402
import voz_descrita  # noqa: E402

FALLOS = []
HECHAS = [0]

VOZ_EL = "AbCdEfGhIjKlMnOpQrSt"            # 20 alfanumericos: forma de ElevenLabs
VOZ_EL_2 = "ZyXwVuTsRqPoNmLkJiHg"
VOZ_CARTESIA = "b042270c-d46f-4d4f-8fb0-7dd7c5fe5615"

GUION = [
    {"id": "S01", "texto": "Octubre de 2007. Un carguero avanza en la oscuridad."},
    {"id": "S02", "texto": "Nadie a bordo sabe lo que esconde la bodega."},
    {"id": "S03", "texto": "Cuando la marina lo aborda, ya es tarde."},
]


def comprobar(condicion, texto):
    HECHAS[0] += 1
    if condicion:
        print(f"  ok   {texto}")
    else:
        print(f"  FALLO {texto}")
        FALLOS.append(texto)


def igual(obtenido, esperado, texto):
    comprobar(obtenido == esperado,
              texto if obtenido == esperado
              else f"{texto}  [obtenido={obtenido!r} esperado={esperado!r}]")


def falla(funcion, texto, tipo=Exception, contiene=None):
    HECHAS[0] += 1
    try:
        funcion()
    except tipo as fallo:
        if contiene and contiene not in str(fallo):
            print(f"  FALLO {texto}  [mensaje sin {contiene!r}: {fallo}]")
            FALLOS.append(texto)
            return None
        print(f"  ok   {texto}")
        return fallo
    except Exception as otro:                                # noqa: BLE001
        print(f"  FALLO {texto}  [excepcion inesperada: {otro!r}]")
        FALLOS.append(texto)
        return None
    print(f"  FALLO {texto}  [no fallo]")
    FALLOS.append(texto)
    return None


# ------------------------------------------------------------ audio de prueba

def pcm_seno(segundos, sr, hz=220.0):
    n = int(segundos * sr)
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / sr)))
                    for i in range(n))


def alineacion(texto, segundos):
    """Alineacion por caracter repartida a lo largo de `segundos`."""
    paso = segundos / max(1, len(texto))
    return {"characters": list(texto),
            "character_start_times_seconds": [round(i * paso, 4) for i in range(len(texto))],
            "character_end_times_seconds": [round((i + 1) * paso, 4) for i in range(len(texto))]}


def wav_de(pcm, sr, canales=1):
    salida = io.BytesIO()
    with wave.open(salida, "wb") as fh:
        fh.setnchannels(canales)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(pcm)
    return salida.getvalue()


def mp3_de(pcm, sr):
    """Un MP3 de verdad hecho con ffmpeg en local, o None si no hay ffmpeg."""
    ejecutable = shutil.which("ffmpeg")
    if not ejecutable:
        return None
    proceso = subprocess.run(
        [ejecutable, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar",
         str(sr), "-ac", "1", "-i", "pipe:0", "-f", "mp3", "-b:a", "128k", "pipe:1"],
        input=pcm, capture_output=True, timeout=120)
    return proceso.stdout if proceso.returncode == 0 else None


class Respuesta:
    def __init__(self, codigo, datos=None, texto="", cabeceras=None):
        self.status_code = codigo
        self._datos = datos
        self.text = texto or (json.dumps(datos) if datos is not None else "")
        self.headers = cabeceras or {}

    def json(self):
        if self._datos is None:
            raise ValueError("sin json")
        return self._datos


def toma_ok(texto, segundos=1.2, sr=24000, audio=None, request_id="req-1"):
    crudo = audio if audio is not None else pcm_seno(segundos, sr)
    return Respuesta(200, {"audio_base64": base64.b64encode(crudo).decode(),
                           "alignment": alineacion(texto, segundos)},
                     cabeceras={"request-id": request_id})


class RedFalsa:
    """Sustituye a `requests` dentro del motor. Apunta cada llamada."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []
        self.ConnectionError = requests.ConnectionError
        self.Timeout = requests.Timeout

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.llamadas.append({"url": url, "headers": dict(headers or {}),
                              "json": json, "params": dict(params or {})})
        siguiente = self.respuestas.pop(0)
        if callable(siguiente):
            return siguiente(url, json, params)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    def get(self, url, headers=None, params=None, timeout=None):
        self.llamadas.append({"url": url, "headers": dict(headers or {}),
                              "params": dict(params or {})})
        return self.respuestas.pop(0)


def motor():
    return p4_voz.motor_de("elevenlabs")


class ConRed:
    """with ConRed([...]) as red: el motor habla con `red` y no con la red."""

    def __init__(self, respuestas):
        self.red = RedFalsa(respuestas)

    def __enter__(self):
        self.m = motor()
        self.antes = (self.m.requests, self.m._dormir)
        self.m.requests = self.red
        self.esperas = []
        self.m._dormir = self.esperas.append
        return self.red

    def __exit__(self, *exc):
        self.m.requests, self.m._dormir = self.antes
        return False


def cfg_el(**extra):
    base = {"proveedor": "elevenlabs", "voz_id": VOZ_EL}
    base.update(extra)
    return p4_voz.resolver_params(base)


# -------------------------------------------------------------------- pruebas

def prueba_formato_en_la_url():
    print("\n[1] el formato de audio va en la URL, no en el cuerpo")
    texto = "Hola mundo, esto es una prueba."
    with ConRed([toma_ok(texto)]) as red:
        wav, duracion, palabras = motor().sintetizar_toma(texto, cfg_el())
    llamada = red.llamadas[0]
    igual(llamada["params"].get("output_format"), "pcm_24000",
          "output_format=pcm_24000 viaja como parametro de consulta")
    comprobar("output_format" not in (llamada["json"] or {}),
              "y NO en el cuerpo, donde la API lo ignora y devuelve MP3")
    comprobar(llamada["url"].endswith(f"/v1/text-to-speech/{VOZ_EL}/with-timestamps"),
              "al endpoint with-timestamps de la voz elegida")
    igual(llamada["json"]["model_id"], "eleven_multilingual_v2", "con el modelo pedido")
    igual(llamada["headers"].get("xi-api-key"), CLAVE_FALSA,
          "la clave va en la cabecera xi-api-key")
    igual(struct.unpack("<I", wav[24:28])[0], 24000, "el WAV sale a 24000 Hz")
    comprobar(abs(duracion - 1.2) < 0.01, f"y dura lo que llego ({duracion:.2f}s)")
    igual([p["w"] for p in palabras], ["Hola", "mundo,", "esto", "es", "una", "prueba."],
          "las marcas son de palabras")


def prueba_validacion_del_audio():
    print("\n[2] se valida lo que LLEGA, no lo que se pidio")
    m = motor()
    sr = 24000
    pcm = pcm_seno(1.0, sr)

    salida, origen = m.decodificar_audio(pcm, "pcm_24000", sr)
    igual(salida, pcm, "PCM crudo de longitud par pasa tal cual")
    igual(origen, "pcm_24000", "y dice de donde sale")

    falla(lambda: m.decodificar_audio(pcm + b"\x01", "pcm_24000", sr),
          "PCM de longitud impar: no se procesa", m.AudioInvalido)
    falla(lambda: m.decodificar_audio(b"", "pcm_24000", sr),
          "audio vacio: error claro", m.AudioInvalido)
    falla(lambda: m.decodificar_audio(b"OggS" + b"\x00" * 100, "pcm_24000", sr),
          "un Ogg no se lee como PCM", m.AudioInvalido)

    # WAV a otra frecuencia y en estereo -> mono a la interna
    estereo = b"".join(struct.pack("<hh", v, v) for v in
                       struct.unpack(f"<{len(pcm_seno(0.5, 44100)) // 2}h",
                                     pcm_seno(0.5, 44100)))
    salida, origen = m.decodificar_audio(wav_de(estereo, 44100, 2), "pcm_24000", sr)
    comprobar(abs(len(salida) / (2 * sr) - 0.5) < 0.01,
              f"un WAV estereo a 44,1 kHz se lee por su cabecera y se pasa a 24 kHz mono "
              f"({len(salida) / (2 * sr):.3f}s)")
    igual(origen, "wav_44100", "y el origen dice que era un WAV")

    # Un PCM que empieza por 0xFF 0xE? NO es un MP3: hace falta otra trama detras
    falso = b"\xff\xfb\x90\x64" + b"\x00" * 2000
    comprobar(not m.es_mp3(falso),
              "una sola cabecera de trama no basta para llamarlo MP3 (sin falsos positivos)")

    mp3 = mp3_de(pcm, sr)
    if mp3 is None:
        print("  (sin ffmpeg: se omite el MP3 de verdad)")
    else:
        comprobar(m.es_mp3(mp3), "un MP3 de verdad se reconoce")
        salida, origen = m.decodificar_audio(mp3, "pcm_24000", sr)
        segundos = len(salida) / (2.0 * sr)
        comprobar(0.9 <= segundos <= 1.2,
                  f"pedir PCM y recibir MP3 se DECODIFICA, no se lee como PCM ({segundos:.2f}s)")
        comprobar("mp3" in origen, f"y se dice ({origen})")
        # leido como PCM habria durado otra cosa: el tamano del MP3 / 48000
        comprobar(abs(len(mp3) / 48000.0 - segundos) > 0.3,
                  "(leido como PCM habria salido una duracion falsa)")
        antes = m._ffmpeg
        m._ffmpeg = lambda: None
        try:
            falla(lambda: m.decodificar_audio(mp3, "pcm_24000", sr),
                  "sin ffmpeg, un MP3 se rechaza en vez de leerse como PCM",
                  m.AudioInvalido, contiene="no se procesa un MP3")
        finally:
            m._ffmpeg = antes

    # frecuencia que no casa con las marcas: 44,1 kHz leidos como 24 kHz
    texto = "Una frase de prueba para medir."
    largo = pcm_seno(1.0, 44100)                       # 1 s real a 44,1 kHz
    with ConRed([toma_ok(texto, segundos=1.0, audio=largo)]):
        falla(lambda: motor().sintetizar_toma(texto, cfg_el()),
              "audio a otra frecuencia que la declarada: se detecta por las marcas",
              m.AudioInvalido, contiene="frecuencia")
    corto = pcm_seno(0.3, 24000)
    with ConRed([toma_ok(texto, segundos=1.5, audio=corto)]):
        falla(lambda: motor().sintetizar_toma(texto, cfg_el()),
              "y tambien cuando el audio se queda corto frente a sus marcas",
              m.AudioInvalido)
    with ConRed([Respuesta(200, {"audio_base64": "", "alignment": {}})]):
        falla(lambda: motor().sintetizar_toma(texto, cfg_el()),
              "una toma sin audio es un error, no un silencio", m.AudioInvalido)
    with ConRed([Respuesta(200, {"audio_base64": base64.b64encode(
            pcm_seno(1.0, 24000)).decode(), "alignment": {}})]):
        falla(lambda: motor().sintetizar_toma(texto, cfg_el()),
              "audio sin marcas de palabra: no se puede sincronizar", m.AudioInvalido)


def prueba_formato_rechazado():
    print("\n[3] si la cuenta no admite el formato, se prueba otro y se convierte")
    m = motor()
    texto = "Una frase de prueba."
    rechazo = Respuesta(403, {"detail": {"status": "output_format_not_allowed",
                                         "message": "pcm_44100 requires Pro tier"}})
    cfg = cfg_el(formato="pcm_44100")
    igual(cfg["sr"], 44100, "pedir pcm_44100 fija la frecuencia interna a 44100")
    with ConRed([rechazo, toma_ok(texto, segundos=1.0, sr=24000)]) as red:
        wav, duracion, palabras = m.sintetizar_toma(texto, cfg)
    igual([c["params"]["output_format"] for c in red.llamadas],
          ["pcm_44100", "pcm_24000"], "rechazado pcm_44100, se pide pcm_24000")
    igual(struct.unpack("<I", wav[24:28])[0], 44100,
          "y lo recibido a 24 kHz se convierte a los 44,1 kHz internos")
    comprobar(abs(duracion - 1.0) < 0.01, f"sin cambiar la duracion ({duracion:.3f}s)")

    mp3 = mp3_de(pcm_seno(1.0, 44100), 44100)
    if mp3 is not None:
        rechazo_24 = Respuesta(422, {"detail": "output_format pcm_24000 not available"})
        with ConRed([rechazo, rechazo_24,
                     toma_ok(texto, segundos=1.0, audio=mp3)]) as red:
            wav, duracion, _ = m.sintetizar_toma(texto, cfg_el(formato="pcm_44100"))
        igual([c["params"]["output_format"] for c in red.llamadas],
              ["pcm_44100", "pcm_24000", "mp3_44100_128"],
              "sin ningun PCM, el ultimo recurso es MP3")
        comprobar(0.9 <= duracion <= 1.2,
                  f"y el MP3 se convierte con ffmpeg ({duracion:.2f}s)")


def prueba_errores_de_la_api():
    print("\n[4] reintentos, clave, voz y limites")
    m = motor()
    texto = "Una frase."
    with ConRed([Respuesta(429, {"detail": "busy"}, cabeceras={"Retry-After": "3"}),
                 toma_ok(texto)]) as red:
        m.sintetizar_toma(texto, cfg_el())
    igual(len(red.llamadas), 2, "un 429 se reintenta")
    with ConRed([Respuesta(500, texto="x")] * 3) as red:
        falla(lambda: m.sintetizar_toma(texto, cfg_el()),
              "tres 5xx seguidos: error claro", RuntimeError, contiene="no contesta")
    with ConRed([requests.ConnectionError("caida"), toma_ok(texto)]) as red:
        m.sintetizar_toma(texto, cfg_el())
    igual(len(red.llamadas), 2, "una caida de red se reintenta")
    with ConRed([Respuesta(401, {"detail": {"message": "invalid api key"}})]):
        fallo = falla(lambda: m.sintetizar_toma(texto, cfg_el()),
                      "clave rechazada: error claro", RuntimeError, contiene="clave")
    comprobar(fallo is None or CLAVE_FALSA not in str(fallo),
              "y el mensaje NO lleva la clave")
    with ConRed([Respuesta(404, {"detail": "voice not found"})]):
        falla(lambda: m.sintetizar_toma(texto, cfg_el()),
              "voz inexistente: dice que se elija otra", RuntimeError, contiene="voz")
    with ConRed([]) as red:
        falla(lambda: m.sintetizar_toma("palabra " * 2000, cfg_el()),
              "pasar el limite de caracteres del modelo falla ANTES de llamar",
              ValueError, contiene="10000")
        falla(lambda: m.sintetizar_toma(texto, dict(cfg_el(), voz_id="")),
              "sin voz elegida no se llama a nadie: se pide elegir una",
              RuntimeError, contiene="Elige una")
    igual(red.llamadas, [], "ninguna de las dos ha tocado la red")
    with ConRed([toma_ok("Hola mundo.")]) as red:
        m.sintetizar_toma('Hola <break time="1s"/> <spell>mundo</spell>.', cfg_el())
    igual(red.llamadas[0]["json"]["text"], "Hola mundo .",
          "las etiquetas SSML no viajan: se locuta el texto limpio")


def prueba_alineacion_y_secciones():
    print("\n[5] alineacion por caracter y tomas por secciones")
    m = motor()
    palabras = m._palabras_de_alineacion(alineacion("Hola — mundo ...", 1.6))
    igual([p["w"] for p in palabras], ["Hola—", "mundo..."],
          "un token de pura puntuacion se pega a la palabra anterior")
    comprobar(all(p["s"] <= p["e"] for p in palabras), "con inicio antes que fin")

    trozos = ["Primera seccion del guion.", "Segunda seccion."]
    with ConRed([toma_ok(trozos[0], segundos=1.5, request_id="a"),
                 toma_ok(trozos[1], segundos=1.0, request_id="b")]) as red:
        wav, duracion, marcas = m.sintetizar_trozos(
            trozos, cfg_el(), anterior="Lo de antes.", siguiente="Lo de despues.")
    comprobar(abs(duracion - 2.5) < 0.01, f"las dos secciones suman su duracion ({duracion:.2f}s)")
    igual(marcas[4]["w"], "Segunda", "la primera palabra de la segunda seccion")
    comprobar(abs(marcas[4]["s"] - 1.5) < 0.01,
              f"empieza donde acaba el audio de la primera ({marcas[4]['s']}s): tiempos absolutos")
    primera, segunda = red.llamadas[0]["json"], red.llamadas[1]["json"]
    igual(primera.get("previous_text"), "Lo de antes.",
          "la primera lleva de contexto lo que va antes de los trozos")
    igual(primera.get("next_text"), "Segunda seccion. Lo de despues.",
          "y lo que viene despues")
    igual(segunda.get("previous_text"), "Lo de antes. Primera seccion del guion.",
          "la segunda, todo lo anterior")
    igual(struct.unpack("<I", wav[24:28])[0], 24000, "una sola pista a 24 kHz")


def prueba_catalogo():
    print("\n[6] el catalogo de voces de la cuenta (/v2/voices)")
    pagina1 = Respuesta(200, {"voices": [
        {"voice_id": VOZ_EL, "name": "Mi narrador", "category": "cloned",
         "labels": {"gender": "male", "accent": "castilian"},
         "verified_languages": [{"language": "es", "locale": "es-ES"}]},
        {"voice_id": VOZ_EL_2, "name": "Serie", "category": "premade",
         "labels": {"language": "en"}}],
        "has_more": True, "next_page_token": "tok2"})
    pagina2 = Respuesta(200, {"voices": [
        {"voice_id": "QqQqQqQqQqQqQqQqQqQq", "name": "Otra", "category": "premade"}],
        "has_more": False, "next_page_token": None})
    with ConRed([pagina1, pagina2]) as red:
        fichas = motor().descargar_voces()
    igual(len(fichas), 3, "pagina hasta el final")
    comprobar(red.llamadas[0]["url"].endswith("/v2/voices"), "contra /v2/voices")
    igual(red.llamadas[1]["params"].get("next_page_token"), "tok2",
          "siguiendo el next_page_token")
    propia = fichas[0]
    comprobar(propia["propia"] and propia["publica"] is False,
              "una voz clonada cuenta como propia de la cuenta")
    igual(propia["idiomas"], ["es"], "con sus idiomas verificados")
    comprobar(not fichas[1]["propia"], "una de serie no es propia")
    ordenadas = p4_voz._filtrar_idioma(fichas, "es")
    igual(ordenadas[0]["id"], VOZ_EL, "al filtrar por idioma, las propias delante")
    igual(len(ordenadas), 3, "y ninguna se esconde (los modelos son multilingues)")

    # en simulado y sin cache, el catalogo de ElevenLabs esta vacio: no se
    # inventa ninguna voz por defecto
    ruta = p4_voz._ruta_cache_voces("elevenlabs")
    if os.path.exists(ruta):
        os.remove(ruta)
    igual(p4_voz.listar_voces("es", proveedor="elevenlabs"), [],
          "sin cache ni red no aparece ninguna voz inventada")


def prueba_configuracion():
    print("\n[7] la configuracion de ElevenLabs")
    cfg = p4_voz.resolver_params({"proveedor": "elevenlabs"})
    igual(cfg["voz_id"], "", "SIN voz por defecto: no se elige ninguna por nadie")
    igual(cfg["modelo"], "eleven_multilingual_v2", "modelo por defecto")
    igual((cfg["formato"], cfg["sr"]), ("pcm_24000", 24000),
          "formato pcm_24000 y frecuencia interna 24000 (no exige plan Pro)")
    igual(cfg["emociones"], [], "sin emociones: no es su vocabulario")
    comprobar(isinstance(cfg["voice_settings"], dict), "con sus voice_settings")
    cfg = p4_voz.resolver_params({"proveedor": "elevenlabs", "preset": "true_crime_tenso",
                                  "voz_id": VOZ_EL})
    igual(cfg["modelo"], "eleven_multilingual_v2",
          "un preset de los de siempre NO cuela su modelo sonic")
    igual(cfg["voz_id"], VOZ_EL, "ni su voz de Cartesia")
    comprobar(cfg["voice_settings"].get("speed") is not None,
              "pero si su velocidad, traducida a speed")
    falla(lambda: p4_voz.resolver_params({"proveedor": "elevenlabs", "formato": "mp3_44100"}),
          "un formato que no es PCM no se acepta como formato interno", ValueError)
    igual(sorted(ajustes.MODELOS_ELEVENLABS), sorted(p4_voz.MODELOS_ELEVENLABS),
          "ajustes y p4_voz tienen los mismos modelos de ElevenLabs")
    igual(sorted(motor().MODELOS), sorted(p4_voz.MODELOS_ELEVENLABS),
          "y el motor tambien")
    igual({k: sorted(v) for k, v in coste.MODELOS_TTS.items()},
          {k: sorted(v) for k, v in p4_voz.MODELOS_POR_PROVEEDOR.items()},
          "y la tabla de costes")
    igual(motor().VOCES, {}, "el motor no trae voces propias (nada de Rachel)")


def prueba_incompatibles():
    print("\n[8] incompatibles: se avisa, no se cambia el proveedor")
    fallo = falla(lambda: p4_voz.resolver_params({"proveedor": "elevenlabs",
                                                  "voz_id": VOZ_CARTESIA}),
                  "voz de Cartesia en ElevenLabs: VozIncompatible", p4_voz.VozIncompatible)
    if fallo is not None:
        igual(fallo.campo, "voz_id", "y dice que el mando a tocar es la voz")
        comprobar("Cartesia" in str(fallo) and "ElevenLabs" in str(fallo),
                  "con los dos proveedores nombrados")
    fallo = falla(lambda: p4_voz.resolver_params({"proveedor": "elevenlabs",
                                                  "modelo": "sonic-3.5"}),
                  "modelo sonic en ElevenLabs: VozIncompatible", p4_voz.VozIncompatible)
    if fallo is not None:
        igual(fallo.campo, "modelo", "campo modelo")
    falla(lambda: p4_voz.resolver_params({"proveedor": "cartesia", "voz_id": VOZ_EL}),
          "y al reves: voz de ElevenLabs en Cartesia", p4_voz.VozIncompatible)
    diag = p4_voz.diagnosticar({"proveedor": "elevenlabs", "voz_id": VOZ_CARTESIA})
    comprobar(not diag["ok"] and diag["avisos"][0]["campo"] == "voz_id",
              "diagnosticar no lanza: devuelve el aviso con su campo")
    igual(diag["proveedor"], "elevenlabs", "y el proveedor sigue siendo el que era")
    diag = p4_voz.diagnosticar({"proveedor": "elevenlabs"})
    comprobar(not diag["ok"], "ElevenLabs sin voz: aviso de elegir una")
    comprobar(p4_voz.diagnosticar({"proveedor": "elevenlabs"}, exigir_voz=False)["ok"],
              "salvo en simulado, que no llama a nadie")
    igual(p4_voz.proveedor_de_voz("v-de-prueba"), None,
          "un id que no es de nadie no se juzga")


def preparar_proyecto(nombre):
    proyecto = Proyecto.crear(BASE, nombre)
    estado = Estado(proyecto)
    estado.set_params("ingesta", {"url": "https://ejemplo/1"})
    estado.completar("ingesta", {"frames": 0})
    estado.set_params("brief", {"angulo": "seco"})
    estado.completar("brief", {"ok": True})
    estado.set_params("guion", {"unidades": {b["id"]: {"texto": b["texto"]}
                                             for b in GUION}})
    carpeta = proyecto.ruta_trabajo("guion")
    with open(os.path.join(carpeta, "guion.json"), "w", encoding="utf-8") as fh:
        json.dump({"titulo": nombre, "guion": GUION}, fh, ensure_ascii=False)
    estado.completar("guion", {"guion": GUION, "bloques": len(GUION)})
    return proyecto, estado


def prueba_proyecto_antiguo():
    print("\n[9] un proyecto de antes sigue en Cartesia")
    proyecto, estado = preparar_proyecto("Proyecto de antes")
    params = {"preset": "documental_sobrio", "hueco_minimo": 1.0}
    estado.set_params("voz", params)
    firma_antes = estado.firma_global("voz")
    cfg = p4_voz.resolver_params(params)
    igual((cfg["proveedor"], cfg["sr"], cfg["modelo"]), ("cartesia", 44100, "sonic-3.5"),
          "sin proveedor: Cartesia, 44,1 kHz y sonic-3.5, como siempre")
    comprobar(cfg["voz_id"] == p4_voz.presets_voz.PRESETS["documental_sobrio"]
              ["voces_sugeridas"][0], "con la voz de su preset")
    salidas = p4_voz.ejecutar(proyecto, params)
    with open(salidas["pista"], "rb") as fh:
        cabecera = fh.read(44)
    igual(struct.unpack("<I", cabecera[24:28])[0], 44100, "la pista sale a 44100 Hz")
    igual(salidas["controles"]["proveedor"], "cartesia", "y la meta dice Cartesia")
    igual(estado.params("voz"), params,
          "los params guardados NO se tocan: nadie les escribe un proveedor")
    igual(estado.firma_global("voz"), firma_antes, "ni cambia su firma")
    # la escucha de Cartesia conserva la firma de siempre
    texto = p4_voz.recortar_texto(GUION, 8, cfg.get("velocidad"))
    import hashlib
    esperada = hashlib.sha256(json.dumps(
        [texto, cfg["modelo"], cfg["voz_id"], cfg["idioma"],
         cfg["experimental_controls"], True],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    ruta = p4_voz.previsualizar(proyecto, params, segundos=8)
    igual(os.path.basename(ruta), f"voz_{esperada}.wav",
          "la escucha de Cartesia usa la MISMA firma que antes: no se vuelve a pagar")
    return proyecto, salidas


def prueba_pipeline_elevenlabs(proyecto_viejo, salidas_viejas):
    print("\n[10] el pipeline simulado con ElevenLabs")
    proyecto, estado = preparar_proyecto("Proyecto ElevenLabs")
    params = {"proveedor": "elevenlabs", "voz_id": VOZ_EL, "hueco_minimo": 1.0}
    estado.set_params("voz", params)
    salidas = p4_voz.ejecutar(proyecto, params)
    with open(salidas["pista"], "rb") as fh:
        cabecera = fh.read(44)
    igual(struct.unpack("<I", cabecera[24:28])[0], 24000, "la pista sale a 24000 Hz")
    igual(salidas["controles"]["proveedor"], "elevenlabs", "la meta dice ElevenLabs")
    igual(salidas.get("sr"), 24000, "y su frecuencia")
    huecos = [salidas["bloques"][i + 1]["t_in"] - salidas["bloques"][i]["t_out"]
              for i in range(len(salidas["bloques"]) - 1)]
    comprobar(all(h >= 0.99 for h in huecos),
              f"el aire entre bloques se respeta a 24 kHz ({[round(h, 2) for h in huecos]})")
    tamano = os.path.getsize(salidas["pista"]) - 44
    comprobar(abs(tamano / (2 * 24000.0) - salidas["duracion"]) < 0.05,
              "la duracion declarada cuadra con los bytes a 24 kHz")
    ruta = p4_voz.previsualizar(proyecto, params, segundos=8)
    otra = p4_voz.previsualizar(proyecto, dict(params, voice_settings={"stability": 0.9}),
                                segundos=8)
    comprobar(ruta != otra, "la escucha cachea por voice_settings: cambiar uno no reutiliza")

    # regrabar UNA seccion de una toma de Cartesia con ElevenLabs mezclaria voces
    meta = {"secciones": [{"id": "SB001", "bloques": ["S01"], "t_in": 0.0, "t_out": 1.0}],
            "controles": {k: v for k, v in salidas_viejas["controles"].items()
                          if k not in ("proveedor", "sr")}}
    falla(lambda: p4_voz.regrabar_seccion(GUION, "SB001", p4_voz.resolver_params(params),
                                          meta, tempfile.mkdtemp(dir=BASE)),
          "regrabar una seccion de una toma de Cartesia con ElevenLabs: se niega",
          RuntimeError, contiene="mezclaria")


def prueba_costes():
    print("\n[11] costes separados por proveedor")
    proyecto, _ = preparar_proyecto("Proyecto costes")
    medidor = coste.Medidor(proyecto)
    m = motor()

    # el instrumentador envuelve funciones de p4_voz: aqui se mide el envoltorio
    # sobre un doble, la unica parte que cuesta dinero es la llamada de red
    def falsa_toma(texto, cfg, progreso=None):
        return b"RIFF", 1.0, [{"w": "hola", "s": 0.0, "e": 0.4}]

    envuelta_el = coste._medir_toma_real(falsa_toma)
    texto = "Hola, esto es lo que se manda a sintetizar."
    with coste.contexto(proyecto, "voz"):
        envuelta_el(texto, cfg_el())
        envuelta_el(texto, cfg_el(modelo="eleven_flash_v2_5"))
        envuelta_el(texto, p4_voz.resolver_params({}))
    eventos = medidor.eventos()
    igual([e["proveedor"] for e in eventos], ["tts_elevenlabs", "tts_elevenlabs", "tts"],
          "ElevenLabs anota como tts_elevenlabs y Cartesia como tts")
    igual(eventos[0]["usd"], round(len(texto) * 8e-05, 6),
          "Multilingual v2 a 0,08 $ por 1.000 caracteres")
    igual(eventos[1]["usd"], round(len(texto) * 4e-05, 6),
          "Flash v2.5 a 0,04 $ por 1.000")
    comprobar(eventos[2]["usd"] == round(len(texto) * 6.5e-05, 6),
              "y Cartesia con SU tarifa, no la de ElevenLabs")
    total = medidor.total()
    comprobar("ElevenLabs" in total["cabecera"] and "Cartesia" in total["cabecera"],
              f"la cabecera las nombra por separado ({total['cabecera']})")
    igual(total["proveedores"]["tts_elevenlabs"]["cantidad"]["caracteres"], 2 * len(texto),
          "cada proveedor con sus caracteres")

    envuelta_ctx = coste._medir_toma_por_contexto(
        lambda trozos, cfg, progreso=None, **kw: (b"RIFF", 1.0, []))
    with coste.contexto(proyecto, "voz"):
        envuelta_ctx(["uno", "dos"], cfg_el(), None, anterior="x", siguiente="y")
    ultimo = medidor.eventos()[-1]
    igual((ultimo["proveedor"], ultimo["cantidad"]["caracteres"]), ("tts_elevenlabs", 6),
          "la toma por secciones tambien, y pasa el contexto sin romperse")

    sin_eventos = coste.agregar([])
    comprobar("ElevenLabs" not in sin_eventos["cabecera"] and "TTS" in sin_eventos["cabecera"],
              "sin nada de ElevenLabs la cabecera es la de siempre")
    tabla = coste.tarifas_de_voz()
    igual(tabla["elevenlabs"]["eleven_flash_v2_5"], 4e-05, "tarifas_de_voz por modelo")
    del m


def prueba_light_y_ajustes():
    print("\n[12] modo light, voz descrita y ajustes de los videos nuevos")
    base = {"nombre": "Canal", "idioma": "es", "tono_prompt": "serio y sin adornos",
            "voz_prompt": "grave y pausada", "estilo_prompt": "dibujo plano y frio",
            "estilo_imagenes": ["referencia.png"]}
    limpio = presets_light.validar_encargo(dict(base, proveedor_voz="elevenlabs",
                                                voz_id=VOZ_EL))
    igual(limpio["proveedor_voz"], "elevenlabs", "el encargo guarda el proveedor")
    falla(lambda: presets_light.validar_encargo(dict(base, proveedor_voz="elevenlabs",
                                                     voz_id=VOZ_CARTESIA)),
          "voz de Cartesia con ElevenLabs en el encargo: se dice antes de lanzar",
          presets_light.ErrorEncargo, contiene="Cartesia")
    falla(lambda: presets_light.validar_encargo(dict(base, proveedor_voz="xai")),
          "un proveedor que no existe se rechaza", presets_light.ErrorEncargo)
    igual(presets_light.validar_encargo(base)["proveedor_voz"], "",
          "sin proveedor: vacio, el de Configuracion")

    propuesta = {"voz_id": VOZ_EL, "voz_nombre": "Mi narrador", "velocidad": "normal",
                 "emociones": ["anger:high"], "hueco_minimo": 1.0}
    validado = voz_descrita._validar(propuesta, {VOZ_EL}, "elevenlabs")
    igual(validado["emociones"], [], "voz descrita en ElevenLabs: sin emociones")
    falla(lambda: voz_descrita.proponer("grave", voz_fija=VOZ_CARTESIA,
                                        proveedor="elevenlabs"),
          "voz fija del otro proveedor: aviso ANTES de llamar al CLI",
          p4_voz.VozIncompatible)
    antes = p4_voz.listar_voces
    pedidos = []
    p4_voz.listar_voces = lambda idioma=None, **kw: (pedidos.append(kw.get("proveedor"))
                                                     or [{"id": VOZ_EL, "nombre": "Mi narrador",
                                                          "propia": True}])
    try:
        lineas, ids = voz_descrita._voces_para_instruccion("es", "", "elevenlabs")
    finally:
        p4_voz.listar_voces = antes
    igual(pedidos, ["elevenlabs"], "el catalogo que se le pasa al CLI es el de ElevenLabs")
    comprobar("VOZ PROPIA" in lineas, "con las propias marcadas")

    igual(ajustes.voz_de_partida(), {"proveedor": "elevenlabs",
                                     "modelo": "eleven_multilingual_v2"},
          "un video nuevo nace en ElevenLabs, sin voz si no se ha elegido ninguna")
    falla(lambda: ajustes.guardar({"voz_elevenlabs": VOZ_CARTESIA}),
          "una voz de Cartesia no vale como voz por defecto de ElevenLabs", ValueError)
    ajustes.guardar({"voz_elevenlabs": VOZ_EL, "voz_elevenlabs_nombre": "Mi narrador"})
    igual(ajustes.voz_de_partida().get("voz_id"), VOZ_EL, "con la elegida, nace con ella")
    ajustes.guardar({"proveedor_voz": "cartesia"})
    igual(ajustes.voz_de_partida(), {"proveedor": "cartesia"},
          "y si se prefiere Cartesia para lo nuevo, Cartesia")
    ajustes.guardar({"proveedor_voz": "elevenlabs"})


def main():
    print(f"carpeta de la prueba: {BASE}")
    try:
        prueba_formato_en_la_url()
        prueba_validacion_del_audio()
        prueba_formato_rechazado()
        prueba_errores_de_la_api()
        prueba_alineacion_y_secciones()
        prueba_catalogo()
        prueba_configuracion()
        prueba_incompatibles()
        proyecto, salidas = prueba_proyecto_antiguo()
        prueba_pipeline_elevenlabs(proyecto, salidas)
        prueba_costes()
        prueba_light_y_ajustes()
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
    print()
    if FALLOS:
        print(f"ELEVENLABS: {len(FALLOS)} de {HECHAS[0]} comprobaciones fallan")
        for texto in FALLOS:
            print(f"  - {texto}")
        return 1
    print(f"ELEVENLABS OK: {HECHAS[0]} comprobaciones pasan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
