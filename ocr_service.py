import io
from typing import Any

import requests

OCR_URL = "https://api.ocr.space/parse/image"
OCR_ATTEMPTS = (
    {"OCREngine": 3},
    {"OCREngine": 2, "language": "auto"},
    {"OCREngine": 1, "language": "rus"},
)


class OcrError(Exception):
    pass


def _build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _call_ocr(
    session: requests.Session,
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    api_key: str,
    engine: int,
    language: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "apikey": api_key,
        "OCREngine": engine,
        "detectOrientation": True,
        "scale": True,
        "isTable": True,
    }
    if language:
        payload["language"] = language

    try:
        response = session.post(
            OCR_URL,
            files={"file": (filename, io.BytesIO(image_bytes), mime_type)},
            data=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise OcrError(f"Не удалось связаться с OCR API: {exc}") from exc

    if response.status_code >= 400:
        details = response.text[:200]
        raise OcrError(f"OCR API вернул ошибку {response.status_code}: {details}")

    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise OcrError(f"OCR API вернул некорректный ответ: {response.text[:200]}") from exc
    if data.get("IsErroredOnProcessing"):
        message = data.get("ErrorMessage") or data.get("ErrorDetails") or "OCR error"
        if isinstance(message, list):
            message = "; ".join(message)
        raise OcrError(str(message))

    return data


def _detect_image_format(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "screenshot.png", "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "screenshot.jpg", "image/jpeg"
    if image_bytes.startswith(b"RIFF") and len(image_bytes) >= 12 and image_bytes[8:12] == b"WEBP":
        return "screenshot.webp", "image/webp"
    return "screenshot.png", "application/octet-stream"


def recognize_image(image_bytes: bytes, api_key: str) -> str:
    if not image_bytes:
        raise OcrError("Пустой файл изображения")

    filename, mime_type = _detect_image_format(image_bytes)
    session = _build_session()
    errors: list[str] = []

    for attempt in OCR_ATTEMPTS:
        engine = attempt["OCREngine"]
        language = attempt.get("language")
        lang_suffix = f", language={language}" if language else ""
        try:
            data = _call_ocr(
                session,
                image_bytes,
                filename,
                mime_type,
                api_key,
                engine,
                language,
            )
            parsed_results = data.get("ParsedResults") or []
            if not parsed_results:
                errors.append(f"engine={engine}{lang_suffix}: OCR не вернул текст")
                continue

            text = parsed_results[0].get("ParsedText", "").strip()
            if not text:
                errors.append(f"engine={engine}{lang_suffix}: на скриншоте не найден текст")
                continue

            return text
        except OcrError as exc:
            errors.append(f"engine={engine}{lang_suffix}: {exc}")

    raise OcrError("Не удалось распознать скриншот. " + " | ".join(errors))
