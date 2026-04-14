"""
hrbot/ai_screening.py

AI-скрининг резюме через YandexGPT API.
Извлекает текст из PDF-резюме кандидата и оценивает соответствие вакансии.
"""

import json
import logging
import os

import fitz  # PyMuPDF
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MODEL = "yandexgpt/latest"

# Максимум символов текста резюме, передаваемых в запросе
RESUME_TEXT_LIMIT = 6000


def _get_iam_token() -> str:
    """
    Возвращает IAM-токен для авторизации в YandexGPT.
    Поддерживает два варианта:
      - YANDEX_IAM_TOKEN   — статичный токен (для разработки / сервисного аккаунта)
      - YANDEX_API_KEY     — API-ключ сервисного аккаунта (передаётся напрямую)
    В production рекомендуется использовать YANDEX_API_KEY.
    """
    token = getattr(settings, "YANDEX_IAM_TOKEN", None) or os.environ.get(
        "YANDEX_IAM_TOKEN"
    )
    if token:
        return token, "iam"

    api_key = getattr(settings, "YANDEX_API_KEY", None) or os.environ.get(
        "YANDEX_API_KEY"
    )
    if api_key:
        return api_key, "api_key"

    raise EnvironmentError(
        "Не задан ни YANDEX_IAM_TOKEN, ни YANDEX_API_KEY в настройках или переменных окружения."
    )


def _get_folder_id() -> str:
    folder_id = getattr(settings, "YANDEX_FOLDER_ID", None) or os.environ.get(
        "YANDEX_FOLDER_ID"
    )
    if not folder_id:
        raise EnvironmentError(
            "Не задан YANDEX_FOLDER_ID в настройках или переменных окружения."
        )
    return folder_id


def extract_text_from_pdf(file_field) -> str:
    """
    Извлекает текст из Django FileField, содержащего PDF.
    Возвращает строку с текстом (усечённую до RESUME_TEXT_LIMIT символов).
    """
    try:
        with file_field.open("rb") as f:
            pdf_bytes = f.read()

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text())
        doc.close()

        full_text = "\n".join(pages_text).strip()
        if not full_text:
            return ""
        return full_text[:RESUME_TEXT_LIMIT]
    except Exception as exc:
        logger.exception("Ошибка при извлечении текста из PDF: %s", exc)
        raise ValueError(f"Не удалось прочитать PDF-резюме: {exc}") from exc


def _build_prompt(resume_text: str, job_title: str, job_description: str) -> str:
    return f"""Ты — опытный HR-специалист. Оцени, насколько кандидат подходит на вакансию.

Вакансия: {job_title}
Описание вакансии:
{job_description or 'Описание не указано.'}

Резюме кандидата:
{resume_text or 'Резюме не предоставлено.'}

Верни ответ СТРОГО в формате JSON (без markdown, без пояснений вне JSON):
{{
  "score": <целое число от 0 до 100>,
  "matched_skills": ["навык1", "навык2"],
  "missing_skills": ["навык3", "навык4"],
  "summary": "<2-3 предложения с оценкой кандидата>"
}}"""


def call_yandex_gpt(prompt: str) -> dict:
    """
    Отправляет запрос в YandexGPT и возвращает распарсенный JSON-ответ.
    """
    token, token_type = _get_iam_token()
    folder_id = _get_folder_id()

    if token_type == "iam":
        auth_header = f"Bearer {token}"
    else:
        auth_header = f"Api-Key {token}"

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }

    payload = {
        "modelUri": f"gpt://{folder_id}/{YANDEX_MODEL}",
        "completionOptions": {
            "stream": False,
            "temperature": 0.2,
            "maxTokens": 1000,
        },
        "messages": [
            {"role": "user", "text": prompt},
        ],
    }

    response = requests.post(
        YANDEX_GPT_URL, headers=headers, json=payload, timeout=30
    )

    if response.status_code != 200:
        logger.error(
            "YandexGPT вернул %s: %s", response.status_code, response.text[:500]
        )
        raise RuntimeError(
            f"Ошибка YandexGPT API: HTTP {response.status_code}. "
            f"Детали: {response.text[:200]}"
        )

    data = response.json()
    raw_text = data["result"]["alternatives"][0]["message"]["text"]

    # Убираем возможные markdown-обёртки ```json ... ```
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```", 2)[-1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("YandexGPT вернул невалидный JSON: %s", raw_text[:500])
        raise ValueError(
            f"YandexGPT вернул ответ в неожиданном формате: {raw_text[:200]}"
        ) from exc


def screen_resume(candidate) -> dict:
    """
    Основная функция скрининга.

    Принимает экземпляр модели Candidate, возвращает словарь:
    {
        "score": int (0–100),
        "matched_skills": list[str],
        "missing_skills": list[str],
        "summary": str,
        "resume_text_length": int,
    }

    Выбрасывает ValueError / RuntimeError / EnvironmentError при ошибках.
    """
    # 1. Получаем описание вакансии
    recruitment = candidate.recruitment_id
    job_title = (
        str(recruitment.job_position_id)
        if recruitment and recruitment.job_position_id
        else "Не указана"
    )
    job_description = (recruitment.description or "") if recruitment else ""

    # 2. Извлекаем текст резюме
    if not candidate.resume:
        raise ValueError("У кандидата не загружено резюме.")

    resume_text = extract_text_from_pdf(candidate.resume)

    # 3. Вызываем YandexGPT
    prompt = _build_prompt(resume_text, job_title, job_description)
    result = call_yandex_gpt(prompt)

    # 4. Валидируем и нормализуем ответ
    score = int(result.get("score", 0))
    score = max(0, min(100, score))

    return {
        "score": score,
        "matched_skills": result.get("matched_skills", []),
        "missing_skills": result.get("missing_skills", []),
        "summary": result.get("summary", ""),
        "resume_text_length": len(resume_text),
    }
