"""Motor de imagen de respaldo con Gemini 3.1 Flash Image.

Usa la API REST de generateContent y las referencias en el mismo orden que
recibe del pipeline. No depende del SDK de Google: requests y Pillow ya existen.
"""
import base64
import os
import time

import requests
from PIL import Image
from io import BytesIO

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
MODELO = "gemini-3.1-flash-image"
ASPECTOS = {"apaisado": "16:9", "cuadrado": "1:1", "vertical": "9:16"}
TAMANO = {"low": "1K", "medium": "2K", "high": "2K"}
# Estimación del precio de salida de imagen publicada por Google; el gasto real
# puede variar según resolución y tokens de entrada/razonamiento.
PRECIO = {"low": 0.067, "medium": 0.101, "high": 0.101}


def generar(prompt, referencias, *, quality="low", tamano="apaisado", api_key=None):
    clave = (api_key or _leer_clave()).strip()
    if not clave:
        raise RuntimeError(
            "OpenAI se quedó sin crédito y no hay clave de Gemini configurada. "
            "Añádela en Configuración > Gemini — imágenes.")
    referencias = list(referencias or [])
    if len(referencias) > 14:
        raise ValueError(
            f"Gemini admite como máximo 14 imágenes de referencia; este plano trae "
            f"{len(referencias)}. Reduce las referencias para poder usar el respaldo.")
    partes = [{"text": str(prompt)}]
    for ruta in referencias:
        if not os.path.isfile(ruta):
            raise ValueError(f"la imagen de referencia no existe: {ruta}")
        with open(ruta, "rb") as fh:
            datos = fh.read()
        mime = "image/jpeg" if ruta.lower().endswith((".jpg", ".jpeg")) else "image/png"
        partes.append({"inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(datos).decode("ascii"),
        }})
    cuerpo = {
        "contents": [{"role": "user", "parts": partes}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {
                "aspectRatio": ASPECTOS.get(tamano, "16:9"),
                "imageSize": TAMANO.get(quality, "1K"),
            },
        },
    }
    inicio = time.time()
    try:
        respuesta = requests.post(
            API_URL, params={"key": clave}, json=cuerpo, timeout=600)
    except requests.RequestException as exc:
        raise RuntimeError(f"no se pudo conectar con Gemini: {exc}") from exc
    if respuesta.status_code != 200:
        try:
            detalle = (respuesta.json().get("error") or {}).get("message") or respuesta.text
        except ValueError:
            detalle = respuesta.text
        raise RuntimeError(
            f"Gemini respondió HTTP {respuesta.status_code}: {str(detalle)[:500]}")
    try:
        payload = respuesta.json()
        candidatos = payload.get("candidates") or []
        partes_salida = ((candidatos[0].get("content") or {}).get("parts") or [])
        imagen = next(
            (p.get("inlineData") or p.get("inline_data")
             for p in partes_salida
             if p.get("inlineData") or p.get("inline_data")), None)
        if not imagen or not imagen.get("data"):
            motivo = ((candidatos[0].get("finishReason") if candidatos else None)
                      or "sin imagen en la respuesta")
            raise RuntimeError(f"Gemini no devolvió una imagen: {motivo}")
        bruto = base64.b64decode(imagen["data"])
        # El pipeline de montaje espera PNG aunque el proveedor devuelva JPEG.
        with Image.open(BytesIO(bruto)) as im:
            salida = BytesIO()
            im.convert("RGB").save(salida, format="PNG")
            png = salida.getvalue()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"respuesta de imagen de Gemini no reconocida: {exc}") from exc
    coste = PRECIO.get(quality, PRECIO["low"])
    return png, {
        "segundos": round(time.time() - inicio, 1),
        "quality": quality,
        "refs": len(referencias),
        "coste": coste,
        "modelo": MODELO,
        "tamano": ASPECTOS.get(tamano, "16:9"),
        "usage": payload.get("usageMetadata") or {},
        "proveedor": "gemini",
    }


def _leer_clave():
    try:
        try:
            from pasos import claves
        except ImportError:
            import claves
        return (claves.leer().get("gemini") or {}).get("clave") or ""
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")
