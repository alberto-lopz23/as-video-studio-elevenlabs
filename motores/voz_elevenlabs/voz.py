"""
Motor de voz: ElevenLabs con marcas de tiempo por palabra.

Usa POST /v1/text-to-speech/{voz}/with-timestamps, que devuelve el audio en
base64 y la alineacion POR CARACTER. La alineacion se convierte a palabras con
el mismo contrato que el motor de Cartesia: {"w", "s", "e"}. El texto que viaja
a la API va sin etiquetas SSML: las marcas tienen que ser de lo que se locuta.

EL FORMATO DE AUDIO VA EN LA URL
--------------------------------
`output_format` es un parametro de CONSULTA, no del cuerpo. Mandado en el
cuerpo se ignora sin error y la API devuelve su defecto, MP3 a 44,1 kHz; leer
eso como PCM da ruido con la duracion equivocada --y la toma ya se ha pagado--.
Por eso aqui:

1. Se pide PCM a 24 kHz (`pcm_24000`), que no exige plan Pro (44,1 kHz si).
2. NO se cree lo que se pidio: se mira lo que llego. Un WAV se lee por su
   cabecera, un MP3 se reconoce por su cabecera de trama y se decodifica con
   ffmpeg, y el PCM crudo tiene que ser de longitud par y durar lo que dicen
   sus propias marcas de tiempo (si no, la frecuencia no es la que se cree).
3. Si la cuenta rechaza el formato pedido, se prueba con `pcm_24000` y, en
   ultimo caso, con MP3 (que admiten todas las cuentas) convertido con ffmpeg.
   Un rechazo de formato es un 4xx y no consume caracteres.
4. Sea cual sea el formato recibido, lo que sale de aqui es PCM s16le mono a
   la frecuencia de `cfg["sr"]`, que es el formato interno del pipeline.

SIN VOZ POR DEFECTO
-------------------
No hay una voz elegida por nosotros: la voz sale del catalogo de la cuenta
(/v2/voices). Sin voz, sintetizar falla con un mensaje que dice que hay que
elegir una; la simulacion (ESTUDIO_SIMULAR) no llega aqui.
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave

import requests

API_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voz_id}/with-timestamps"
API_VOCES = "https://api.elevenlabs.io/v2/voices"
PROVEEDOR = "elevenlabs"

MODELO = "eleven_multilingual_v2"
#: Los modelos que se ofrecen. Turbo v2.5 esta marcado como obsoleto por
#: ElevenLabs (equivale a Flash v2.5); se acepta para no romper un ajuste
#: guardado, pero la interfaz recomienda Flash.
MODELOS = ("eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5")
NOMBRES_MODELO = {
    "eleven_multilingual_v2": "Multilingual v2 (calidad)",
    "eleven_flash_v2_5": "Flash v2.5 (rapido y barato)",
    "eleven_turbo_v2_5": "Turbo v2.5 (obsoleto: usa Flash)",
}
#: Caracteres por peticion, de la tabla de modelos de ElevenLabs.
LIMITE_CARACTERES = {
    "eleven_multilingual_v2": 10000,
    "eleven_flash_v2_5": 40000,
    "eleven_turbo_v2_5": 40000,
}

#: Formato interno del pipeline y el que se pide por defecto.
SR = 24000
FORMATO = "pcm_24000"
FORMATOS_PCM = {"pcm_16000": 16000, "pcm_22050": 22050, "pcm_24000": 24000,
                "pcm_44100": 44100, "pcm_48000": 48000}
#: Ultimo recurso si la cuenta no admite PCM: lo admiten todas.
FORMATO_RESPALDO = "mp3_44100_128"

#: Sin voces propias de este motor (ver cabecera). Se deja el nombre porque
#: forma parte del contrato que lee p4_voz.
VOCES = {}

REINTENTOS = 3
ESPERA_BASE_S = 2.0
_dormir = time.sleep             # sustituible en las pruebas

CARPETA_SECRETOS = os.environ.get("ESTUDIO_SECRETOS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "secretos")
RUTAS_ENV = [os.path.join(CARPETA_SECRETOS, ".env")]
RUTA_CLAVES = os.path.join(CARPETA_SECRETOS, "claves.json")


class AudioInvalido(RuntimeError):
    """La API contesto 200 pero lo que llego no es audio usable."""


# ------------------------------------------------------------------ la clave

def _del_almacen():
    if not os.path.exists(RUTA_CLAVES):
        return ""
    try:
        with open(RUTA_CLAVES, "r", encoding="utf-8-sig") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return ""
    ficha = datos.get("elevenlabs") if isinstance(datos, dict) else None
    if isinstance(ficha, dict):
        return str(ficha.get("clave") or "").strip()
    if isinstance(ficha, str):
        return ficha.strip()
    return ""


def cargar_api_key():
    """Entorno, almacen del Estudio o .env. Nunca por linea de comandos."""
    clave = os.environ.get("ELEVENLABS_API_KEY")
    if clave:
        return clave.strip()
    clave = _del_almacen()
    if clave:
        return clave
    for ruta in RUTAS_ENV:
        if not os.path.exists(ruta):
            continue
        with open(ruta, "r", encoding="utf-8-sig") as fh:
            for linea in fh:
                m = re.match(r"^ELEVENLABS_API_KEY=(.*)$", linea.strip())
                if m:
                    return m.group(1).strip()
    raise SystemExit(
        "No encuentro ELEVENLABS_API_KEY. Ponla en la pantalla de Configuracion "
        f"(se guarda en {RUTA_CLAVES}), o en el entorno.")


# ------------------------------------------------------------------ el audio

def wav_desde_pcm(pcm: bytes, sr: int = SR) -> bytes:
    """Cabecera WAV PCM s16le mono sobre PCM crudo."""
    sr = int(sr)
    cabecera = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    cabecera += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    cabecera += b"data" + struct.pack("<I", len(pcm))
    return cabecera + pcm


def sr_de_formato(formato):
    """Frecuencia de un output_format de ElevenLabs ('pcm_24000' -> 24000)."""
    m = re.match(r"^[a-z0-9]+_(\d{4,6})(?:_\d+)?$", str(formato or ""))
    return int(m.group(1)) if m else None


_BITRATES_MPEG1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224,
                      256, 320, 0)
_BITRATES_MPEG2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144,
                      160, 0)
_FRECUENCIAS_MPEG = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000),
                     0: (11025, 12000, 8000)}


def _trama_mp3(datos, pos):
    """Longitud de la trama MP3 (capa III) que empieza en pos, o None."""
    if pos + 4 > len(datos):
        return None
    b1, b2 = datos[pos + 1], datos[pos + 2]
    if datos[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03            # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reservado
    capa = (b1 >> 1) & 0x03               # 1 = capa III
    if version == 1 or capa != 1:
        return None
    indice_bit = (b2 >> 4) & 0x0F
    indice_sr = (b2 >> 2) & 0x03
    if indice_bit in (0, 15) or indice_sr == 3:
        return None
    relleno = (b2 >> 1) & 0x01
    sr = _FRECUENCIAS_MPEG[version][indice_sr]
    if version == 3:
        kbps = _BITRATES_MPEG1_L3[indice_bit]
        return 144000 * kbps // sr + relleno
    kbps = _BITRATES_MPEG2_L3[indice_bit]
    return 72000 * kbps // sr + relleno


def es_mp3(datos):
    """True si los bytes son un MP3: etiqueta ID3, o DOS tramas encadenadas.

    Pedir dos tramas seguidas y no una es lo que evita el falso positivo: un PCM
    crudo puede empezar por 0xFF 0xEx por casualidad, pero no tener ademas otra
    cabecera valida justo donde la primera dice que acaba.
    """
    if datos[:3] == b"ID3":
        return True
    largo = _trama_mp3(datos, 0)
    if not largo:
        return False
    return _trama_mp3(datos, largo) is not None


def _ffmpeg():
    puesto = (os.environ.get("ESTUDIO_FFMPEG") or "").strip().strip('"')
    if puesto and os.path.isfile(puesto):
        return puesto
    return shutil.which("ffmpeg")


def _mp3_a_pcm(datos, sr):
    ejecutable = _ffmpeg()
    if not ejecutable:
        raise AudioInvalido(
            "ElevenLabs devolvio MP3 y no encuentro ffmpeg para convertirlo; "
            "no se procesa un MP3 como si fuera PCM")
    proceso = subprocess.run(
        [ejecutable, "-hide_banner", "-loglevel", "error", "-f", "mp3",
         "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
         "-ar", str(int(sr)), "pipe:1"],
        input=datos, capture_output=True, timeout=300)
    if proceso.returncode != 0 or not proceso.stdout:
        raise AudioInvalido("ffmpeg no ha podido decodificar el MP3 de "
                            f"ElevenLabs: {proceso.stderr[-300:]!r}")
    return proceso.stdout


def _wav_a_pcm(datos):
    try:
        with wave.open(io.BytesIO(datos), "rb") as fh:
            canales, ancho, sr = fh.getnchannels(), fh.getsampwidth(), fh.getframerate()
            crudo = fh.readframes(fh.getnframes())
    except (wave.Error, EOFError) as fallo:
        raise AudioInvalido(f"WAV ilegible de ElevenLabs: {fallo}")
    if ancho != 2:
        raise AudioInvalido(f"WAV de {ancho * 8} bits; se esperaban 16")
    if canales > 1:
        import numpy as np
        muestras = np.frombuffer(crudo, dtype="<i2").reshape(-1, canales)
        crudo = muestras.mean(axis=1).astype("<i2").tobytes()
    return crudo, sr


def remuestrear(pcm, sr_origen, sr_destino):
    """PCM s16le mono de una frecuencia a otra (interpolacion lineal)."""
    sr_origen, sr_destino = int(sr_origen), int(sr_destino)
    if sr_origen == sr_destino or not pcm:
        return pcm
    import numpy as np
    origen = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n_destino = max(1, int(round(len(origen) * sr_destino / sr_origen)))
    x_destino = np.arange(n_destino) * (sr_origen / sr_destino)
    destino = np.interp(x_destino, np.arange(len(origen)), origen)
    return np.clip(np.round(destino), -32768, 32767).astype("<i2").tobytes()


def decodificar_audio(datos, formato_pedido, sr_destino):
    """Bytes recibidos -> (pcm s16le mono a sr_destino, descripcion del origen).

    Se mira lo que LLEGO, no lo que se pidio.
    """
    if not datos:
        raise AudioInvalido("ElevenLabs devolvio la toma sin audio")
    if datos[:4] == b"RIFF" and datos[8:12] == b"WAVE":
        pcm, sr = _wav_a_pcm(datos)
        return remuestrear(pcm, sr, sr_destino), f"wav_{sr}"
    if es_mp3(datos):
        if str(formato_pedido).startswith("pcm_"):
            # Se pidio PCM y llego MP3: la API no ha respetado el formato. Se
            # convierte bien, sin leerlo como PCM, pero se dice.
            origen = f"mp3 (se pidio {formato_pedido})"
        else:
            origen = "mp3"
        return _mp3_a_pcm(datos, sr_destino), origen
    if datos[:4] in (b"OggS", b"fLaC"):
        raise AudioInvalido(f"ElevenLabs devolvio {datos[:4].decode()} y se "
                            f"pidio {formato_pedido}")
    sr = FORMATOS_PCM.get(str(formato_pedido))
    if sr is None:
        raise AudioInvalido(f"llegaron bytes que no son WAV ni MP3 y el formato "
                            f"pedido ({formato_pedido}) no es PCM")
    if len(datos) % 2:
        raise AudioInvalido("PCM de longitud impar: no es s16le, no se procesa")
    return remuestrear(datos, sr, sr_destino), str(formato_pedido)


def validar_duracion(pcm, sr, palabras):
    """La duracion del audio tiene que casar con sus propias marcas.

    Es la comprobacion que caza una frecuencia mal supuesta: 44,1 kHz leido como
    24 kHz dura 1,8 veces mas de lo que dicen las marcas, y al reves se queda
    corto. Un poco de silencio al final es normal; el doble no.
    """
    duracion = len(pcm) / (2.0 * int(sr))
    if not palabras:
        return duracion
    fin = float(palabras[-1]["e"])
    if fin > duracion * 1.15 + 0.3:
        raise AudioInvalido(
            f"el audio dura {duracion:.2f}s y las marcas llegan a {fin:.2f}s: la "
            f"frecuencia de muestreo no es la que se cree; no se usa la toma")
    # El otro sentido, con margen para el silencio final que mete el modelo:
    # solo cuenta si hay frase suficiente (>= 1 s) y sobra mas de medio segundo.
    if fin >= 1.0 and duracion > fin * 1.5 and duracion - fin > 0.6:
        raise AudioInvalido(
            f"el audio dura {duracion:.2f}s y la ultima palabra acaba en "
            f"{fin:.2f}s: la frecuencia de muestreo no es la que se cree")
    return duracion


# --------------------------------------------------------- palabras y marcas

def _sin_etiquetas(texto):
    """Quita etiquetas SSML conservando lo de dentro, y colapsa espacios."""
    limpio = re.sub(r"<[^>]*>", " ", str(texto or ""))
    return re.sub(r"\s+", " ", limpio).strip()


def _normalizar(palabra):
    return re.sub(r"[^\wáéíóúüñ]", "", palabra.lower())


def _palabras_de_alineacion(alineacion, desde=0.0):
    """Alineacion por caracter -> [{"w", "s", "e"}] por palabra.

    Un token que es solo puntuacion («—», «...») no es una palabra que se
    pronuncie: se pega a la anterior para que el reparto por escenas no lo
    cuente como una palabra del guion.
    """
    caracteres = alineacion.get("characters") or []
    inicios = alineacion.get("character_start_times_seconds") or []
    finales = alineacion.get("character_end_times_seconds") or []
    crudas, actual, inicio, ultimo = [], [], None, None
    for caracter, ini, fin in zip(caracteres, inicios, finales):
        if str(caracter).isspace():
            if actual:
                crudas.append(("".join(actual), inicio, ultimo))
                actual, inicio = [], None
            continue
        if inicio is None:
            inicio = float(ini)
        actual.append(str(caracter))
        ultimo = float(fin)
    if actual:
        crudas.append(("".join(actual), inicio, ultimo))
    palabras = []
    for texto, ini, fin in crudas:
        if not _normalizar(texto) and palabras:
            palabras[-1]["w"] += texto
            palabras[-1]["e"] = round(fin + desde, 3)
            continue
        palabras.append({"w": texto, "s": round(ini + desde, 3),
                         "e": round(fin + desde, 3)})
    return palabras


def _repartir_palabras(escenas, palabras):
    """El mismo emparejador tolerante que el motor de Cartesia (ver alli)."""
    reparto = {}
    i = 0
    for escena in escenas:
        esperadas = [_normalizar(p) for p in (escena.get("narracion") or "").split()]
        esperadas = [p for p in esperadas if p]
        tramo = []
        for esperada in esperadas:
            j = i
            while j < min(i + 3, len(palabras)):
                if _normalizar(palabras[j]["w"]) == esperada:
                    break
                j += 1
            if j < min(i + 3, len(palabras)):
                tramo.extend(palabras[i:j + 1])
                i = j + 1
            elif i < len(palabras):
                tramo.append(palabras[i])
                i += 1
        reparto[escena["id"]] = tramo
    if i < len(palabras) and reparto:
        ultimo = [e["id"] for e in escenas if (e.get("narracion") or "").strip()]
        if ultimo:
            reparto[ultimo[-1]].extend(palabras[i:])
    return reparto


def _pcm_de_wav(wav):
    return wav[44:]


FUNDIDO_MS = 12


def _relleno_de_sala(pcm, desde_seg, hasta_seg, duracion, sr=SR):
    """Ruido de sala sacado de la propia pausa (ver el motor de Cartesia)."""
    bytes_por_seg = int(sr) * 2
    necesarios = int(duracion * bytes_por_seg) & ~1
    if necesarios <= 0:
        return b""
    ancho = max(0.0, hasta_seg - desde_seg)
    if ancho < 0.04:
        return b"\x00" * necesarios
    toma = min(0.2, ancho * 0.8)
    centro = (desde_seg + hasta_seg) / 2.0
    ini = int((centro - toma / 2) * bytes_por_seg) & ~1
    fin = ini + (int(toma * bytes_por_seg) & ~1)
    semilla = pcm[max(0, ini):min(len(pcm), fin)]
    if len(semilla) < 4:
        return b"\x00" * necesarios

    reverso = semilla[::-1]
    reverso = b"".join(reverso[i:i + 2][::-1]
                       for i in range(0, len(reverso) - 1, 2))
    trozos, largo, vuelta = [], 0, 0
    while largo < necesarios:
        pieza = semilla if vuelta % 2 == 0 else reverso
        trozos.append(pieza)
        largo += len(pieza)
        vuelta += 1
    relleno = bytearray(b"".join(trozos)[:necesarios])

    muestras = FUNDIDO_MS * int(sr) // 1000
    for i in range(min(muestras, len(relleno) // 2)):
        factor = i / muestras
        for pos in (i * 2, len(relleno) - 2 - i * 2):
            valor = int.from_bytes(relleno[pos:pos + 2], "little", signed=True)
            relleno[pos:pos + 2] = int(valor * factor).to_bytes(
                2, "little", signed=True)
    return bytes(relleno)


def espaciar(wav, palabras, reparto, escenas, hueco_minimo=1.0, sr=SR):
    """Ensancha los silencios ENTRE escenas (mismo contrato que Cartesia)."""
    pcm = _pcm_de_wav(wav)
    bytes_por_seg = int(sr) * 2
    cortes = []
    con_voz = [e for e in escenas if reparto.get(e["id"])]
    for anterior, siguiente in zip(con_voz, con_voz[1:]):
        fin = reparto[anterior["id"]][-1]["e"]
        inicio = reparto[siguiente["id"]][0]["s"]
        falta = hueco_minimo - (inicio - fin)
        if falta > 0.01:
            cortes.append((fin + (inicio - fin) / 2, falta, fin, inicio))
    if not cortes:
        return wav, []

    trozos = []
    anterior_byte = 0
    acumulado = 0.0
    desplazamientos = []
    for instante, silencio, desde, hasta in cortes:
        corte_byte = int(instante * bytes_por_seg) & ~1
        trozos.append(pcm[anterior_byte:corte_byte])
        trozos.append(_relleno_de_sala(pcm, desde, hasta, silencio, sr))
        anterior_byte = corte_byte
        acumulado += silencio
        desplazamientos.append({"desde": instante,
                                "retardo": round(acumulado, 3)})
    trozos.append(pcm[anterior_byte:])
    return wav_desde_pcm(b"".join(trozos), sr), desplazamientos


def aplicar_desplazamiento(instante, desplazamientos):
    retardo = 0.0
    for d in desplazamientos:
        if instante >= d["desde"]:
            retardo = d["retardo"]
    return round(instante + retardo, 3)


# ---------------------------------------------------------------- la llamada

def _cuerpo(texto, cfg, anterior="", siguiente=""):
    """Cuerpo JSON. El formato NO va aqui: va en la URL (ver cabecera)."""
    cuerpo = {
        "text": texto,
        "model_id": cfg.get("modelo") or MODELO,
    }
    ajustes = cfg.get("voice_settings")
    if isinstance(ajustes, dict) and ajustes:
        cuerpo["voice_settings"] = dict(ajustes)
    if anterior:
        cuerpo["previous_text"] = anterior[-1000:]
    if siguiente:
        cuerpo["next_text"] = siguiente[:1000]
    return cuerpo


def _voz_de(cfg):
    voz = str((cfg or {}).get("voz_id") or "").strip()
    if not voz:
        raise RuntimeError(
            "No hay voz de ElevenLabs elegida. Elige una de las voces de tu "
            "cuenta en el paso Voz (o una voz por defecto en Configuracion).")
    return voz


def _comprobar_largo(texto, modelo):
    limite = LIMITE_CARACTERES.get(modelo)
    if limite and len(texto) > limite:
        raise ValueError(
            f"{len(texto)} caracteres en una sola peticion y {modelo} admite "
            f"{limite}: parte el guion en mas secciones o usa Flash v2.5")


def _texto_corto(respuesta):
    try:
        datos = respuesta.json()
        detalle = datos.get("detail") if isinstance(datos, dict) else None
        if isinstance(detalle, dict):
            return str(detalle.get("message") or detalle.get("status") or detalle)[:300]
        if detalle:
            return str(detalle)[:300]
    except ValueError:
        pass
    return (respuesta.text or "")[:300]


def _rechaza_formato(respuesta):
    """Un 4xx que habla del formato o del plan: se puede probar con otro."""
    if respuesta.status_code not in (400, 402, 403, 422):
        return False
    texto = _texto_corto(respuesta).lower()
    return any(p in texto for p in ("output_format", "output format", "format",
                                    "tier", "subscription", "plan"))


def _avisador(progreso):
    """Callback de progreso que acepta None como «sin cambiar la fraccion».

    Los pasos acotan la fraccion con min/max, asi que nunca se les pasa None.
    """
    if getattr(progreso, "_de_elevenlabs", False):
        return progreso
    destino = progreso if callable(progreso) else (lambda *a, **k: None)
    estado = {"f": 0.0}

    def avisa(fraccion, mensaje=""):
        if fraccion is None:
            fraccion = estado["f"]
        estado["f"] = float(fraccion)
        return destino(estado["f"], mensaje)
    avisa._de_elevenlabs = True
    return avisa


def _post(url, cabeceras, cuerpo, formato, avisa):
    """POST con reintentos para 429, 5xx y caidas de red. Nada mas se reintenta."""
    ultimo = None
    for intento in range(REINTENTOS):
        try:
            respuesta = requests.post(url, headers=cabeceras, json=cuerpo,
                                      params={"output_format": formato},
                                      timeout=(30, 600))
        except (requests.ConnectionError, requests.Timeout) as fallo:
            ultimo = f"sin red: {type(fallo).__name__}"
            respuesta = None
        if respuesta is not None and respuesta.status_code not in (429, 500, 502,
                                                                   503, 504):
            return respuesta
        if respuesta is not None:
            ultimo = f"HTTP {respuesta.status_code}: {_texto_corto(respuesta)}"
        if intento + 1 < REINTENTOS:
            espera = ESPERA_BASE_S * (2 ** intento)
            if respuesta is not None:
                try:
                    espera = max(espera, float(respuesta.headers.get("Retry-After")))
                except (TypeError, ValueError):
                    pass
            avisa(None, f"ElevenLabs: {ultimo}; reintento en {espera:.0f}s")
            _dormir(espera)
    raise RuntimeError(f"ElevenLabs no contesta tras {REINTENTOS} intentos ({ultimo})")


def pedir_toma(texto, cfg, anterior="", siguiente="", progreso=None):
    """Una peticion -> (pcm s16le mono a cfg['sr'], palabras, informe)."""
    avisa = _avisador(progreso)
    texto = _sin_etiquetas(texto)
    if not texto:
        raise ValueError("no hay texto que sintetizar")
    modelo = cfg.get("modelo") or MODELO
    _comprobar_largo(texto, modelo)
    voz_id = _voz_de(cfg)
    sr_destino = int(cfg.get("sr") or SR)
    pedido = str(cfg.get("formato") or FORMATO)
    candidatos = [pedido]
    for extra in (FORMATO, FORMATO_RESPALDO):
        if extra not in candidatos:
            candidatos.append(extra)
    api_key = cargar_api_key()
    cabeceras = {"xi-api-key": api_key, "Content-Type": "application/json"}
    url = API_TTS.format(voz_id=voz_id)
    cuerpo = _cuerpo(texto, cfg, anterior, siguiente)
    respuesta, formato = None, pedido
    for formato in candidatos:
        respuesta = _post(url, cabeceras, cuerpo, formato, avisa)
        if respuesta.status_code == 200:
            break
        if _rechaza_formato(respuesta) and formato != candidatos[-1]:
            avisa(None, f"tu cuenta de ElevenLabs no admite {formato}; "
                        f"pruebo otro formato")
            continue
        if respuesta.status_code in (401, 403):
            raise RuntimeError(f"ElevenLabs no acepta la clave "
                               f"(HTTP {respuesta.status_code}): "
                               f"{_texto_corto(respuesta)}")
        if respuesta.status_code == 404:
            raise RuntimeError(f"ElevenLabs no encuentra la voz {voz_id!r} en tu "
                               f"cuenta (HTTP 404). Elige otra en el paso Voz.")
        raise RuntimeError(f"ElevenLabs HTTP {respuesta.status_code}: "
                           f"{_texto_corto(respuesta)}")
    try:
        datos = respuesta.json()
    except ValueError:
        raise AudioInvalido("ElevenLabs contesto 200 sin JSON: el endpoint "
                            "with-timestamps devuelve audio en base64 dentro de un JSON")
    audio = datos.get("audio_base64") if isinstance(datos, dict) else None
    if not audio:
        raise AudioInvalido("ElevenLabs devolvio la toma sin audio")
    try:
        crudo = base64.b64decode(audio, validate=True)
    except (ValueError, TypeError):
        raise AudioInvalido("el audio de ElevenLabs no es base64 valido")
    pcm, origen = decodificar_audio(crudo, formato, sr_destino)
    alineacion = datos.get("alignment") or datos.get("normalized_alignment") or {}
    palabras = _palabras_de_alineacion(alineacion)
    if not palabras:
        raise AudioInvalido("ElevenLabs devolvio audio sin marcas de palabra: "
                            "sin marcas no se puede sincronizar el montaje")
    duracion = validar_duracion(pcm, sr_destino, palabras)
    informe = {"formato_pedido": pedido, "formato_usado": formato,
               "origen": origen, "sr": sr_destino,
               "caracteres": len(texto), "duracion": round(duracion, 3),
               "request_id": (respuesta.headers or {}).get("request-id")}
    return pcm, palabras, informe


def sintetizar_toma(texto, cfg, progreso=None):
    """Una toma continua -> (wav, duracion, palabras)."""
    avisa = _avisador(progreso)
    avisa(0.1, "pidiendo la voz a ElevenLabs")
    pcm, palabras, informe = pedir_toma(texto, cfg, progreso=avisa)
    sr = informe["sr"]
    avisa(0.99, f"{len(palabras)} palabras ({informe['origen']})")
    return wav_desde_pcm(pcm, sr), len(pcm) / (sr * 2), palabras


def sintetizar_trozos(trozos, cfg, progreso=None, anterior="", siguiente=""):
    """Varias secciones con el texto vecino como contexto -> una sola toma.

    Las marcas de cada seccion se desplazan por la duracion REAL de lo anterior
    (del PCM, no de las marcas), asi que los tiempos quedan absolutos.
    `anterior`/`siguiente` son el texto de fuera de los trozos: al regrabar una
    seccion suelta es lo que mantiene la entonacion con sus vecinas.
    """
    avisa = _avisador(progreso)
    limpios = [_sin_etiquetas(t) for t in trozos]
    sr = int(cfg.get("sr") or SR)
    pcm, palabras, duracion = [], [], 0.0
    vivos = [i for i, t in enumerate(limpios) if t]
    for orden, indice in enumerate(vivos):
        antes = " ".join([_sin_etiquetas(anterior)] + limpios[:indice]).strip()
        despues = " ".join(limpios[indice + 1:] + [_sin_etiquetas(siguiente)]).strip()
        avisa(orden / max(1, len(vivos)),
              f"grabando seccion {orden + 1} de {len(vivos)}")
        crudo, marcas, _ = pedir_toma(limpios[indice], cfg, antes, despues,
                                      progreso=avisa)
        for marca in marcas:
            palabras.append({"w": marca["w"],
                             "s": round(marca["s"] + duracion, 3),
                             "e": round(marca["e"] + duracion, 3)})
        pcm.append(crudo)
        duracion += len(crudo) / (sr * 2)
    if not pcm:
        raise RuntimeError("no hay texto que sintetizar")
    avisa(0.99, f"{len(palabras)} palabras")
    return wav_desde_pcm(b"".join(pcm), sr), duracion, palabras


# ------------------------------------------------------------- el catalogo

def ficha_voz(cruda):
    """Una voz de /v2/voices -> la ficha que usa el Estudio."""
    etiquetas = cruda.get("labels") or {}
    categoria = str(cruda.get("category") or "").strip().lower()
    verificados = [v for v in (cruda.get("verified_languages") or [])
                   if isinstance(v, dict)]
    idiomas = []
    for v in verificados:
        clave = str(v.get("language") or "").strip().lower()
        if clave and clave not in idiomas:
            idiomas.append(clave)
    etiqueta_idioma = str(etiquetas.get("language") or "").strip().lower()
    if etiqueta_idioma and etiqueta_idioma not in idiomas:
        idiomas.insert(0, etiqueta_idioma)
    locales = [str(v.get("locale") or "").strip() for v in verificados
               if v.get("locale")]
    return {
        "id": cruda.get("voice_id"),
        "nombre": cruda.get("name") or cruda.get("voice_id"),
        "descripcion": str(cruda.get("description") or etiquetas.get("description")
                           or etiquetas.get("use_case") or "").strip(),
        "idioma": idiomas[0] if idiomas else "",
        "idiomas": idiomas,
        "genero": etiquetas.get("gender") or "",
        "pais": etiquetas.get("accent") or "",
        "locales": locales or idiomas,
        "locales_nativos": idiomas[:1],
        "categoria": categoria,
        # «propia»: la ha creado, clonado o guardado la cuenta. Las premade
        # son las de serie de ElevenLabs.
        "propia": categoria not in ("premade", "default", ""),
        "pro": categoria == "professional",
        "publica": categoria in ("premade", "default", ""),
        "proveedor": PROVEEDOR,
        "muestra": cruda.get("preview_url") or "",
    }


def descargar_voces(api_key=None, paginas=40):
    """Las voces de la cuenta, paginando /v2/voices. Solo lectura, sin coste."""
    api_key = api_key or cargar_api_key()
    fichas, vistas, token = [], set(), None
    for _ in range(paginas):
        params = {"page_size": 100, "include_total_count": "false"}
        if token:
            params["next_page_token"] = token
        respuesta = requests.get(API_VOCES, headers={"xi-api-key": api_key},
                                 params=params, timeout=60)
        if respuesta.status_code != 200:
            raise RuntimeError(f"ElevenLabs /v2/voices HTTP "
                               f"{respuesta.status_code}: {_texto_corto(respuesta)}")
        datos = respuesta.json()
        for cruda in datos.get("voices") or []:
            ficha = ficha_voz(cruda)
            if ficha["id"] and ficha["id"] not in vistas:
                vistas.add(ficha["id"])
                fichas.append(ficha)
        token = datos.get("next_page_token")
        if not datos.get("has_more") or not token:
            break
    return fichas


def main():
    parser = argparse.ArgumentParser(description="ElevenLabs: lista las voces de la cuenta")
    parser.add_argument("--voces", action="store_true")
    args = parser.parse_args()
    if args.voces:
        for ficha in descargar_voces():
            print(f"{ficha['id']}  {ficha['nombre']}  [{','.join(ficha['idiomas'])}] "
                  f"{ficha['categoria']}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
