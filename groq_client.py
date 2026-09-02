"""
Обёртка над Groq API:
- categorize_text  — LLM подбирает категорию из списка (fallback, если
  парсер по ключевым словам и category_map не справились)
- transcribe_voice  — Whisper large-v3, голос -> текст
- extract_receipt_total — Vision (qwen3.6-27b), фото чека -> сумма

Changelog:
- v1.5: parse_question: item-примеры дополнены топливом ("бензин",
        "топливо", "дизель", "солярка") + правило, что глагол без
        существительного ("заправлял опель?") тоже подразумевает
        item="бензин". В проде "когда покупал бензин на опель?" (с явным
        словом "бензин"!) всё равно не давал item — ненадёжность LLM
        подстрахована ещё и детерминированным фоллбэком в insights.py
        (_guess_auto_keyword, v1.4).
- v1.4: parse_question получил intent=quantity ("сколько мороженого я
        купил?") — отдельно от intent=spending, явное правило-разграничение
        в промпте, т.к. по-русски оба вопроса начинаются одинаково
        ("сколько..."), но отвечают на них по-разному (см. insights.py
        _answer_quantity против _answer_money).
- v1.3: parse_question: правило про item расширено с "товар/продукт" до
        явно включающего детали/узлы/жидкости автомобиля — в проде "когда
        менялись рычаги на Матиз?" не давало item вообще (примеры в
        промпте были только про еду), из-за чего last_date падал в
        безключевой фоллбэк (последнее событие по машине ЛЮБОГО типа,
        не именно про рычаги). Теперь явный пример именно этого случая.
- v1.2: parse_question получил intent=last_date — вопросы "когда" ("когда
        менял масло на опеле?"), см. docstring parse_question и insights.py.
- v1.1: TEXT_MODEL переключён на gpt-oss-20b (быстрее/дешевле, чем 120b,
        достаточно для тривиальной классификации в одно слово) +
        reasoning_effort="low" — gpt-oss — reasoning-модель, часть токенов
        по умолчанию уходит на внутреннее рассуждение ДО финального ответа,
        а старый max_tokens=20 (нормальный для прежней не-reasoning модели)
        мог обрезать ответ до того, как модель успевала написать саму
        категорию. Добавлено логирование сырого ответа для диагностики.
        requirements.txt: groq bumped 0.13.0 -> 1.6.0 — старая версия SDK
        вообще не знает про reasoning_effort (строго типизированный create(),
        без **kwargs) и упала бы с TypeError.
"""
import base64
import json
import logging
import os
import re
from groq import Groq

from config import GROQ_API_KEY

client = Groq(api_key=GROQ_API_KEY)

# llama-4-scout-17b-16e-instruct отключена Groq 17 июня 2026 (см.
# console.groq.com/docs/deprecations). ВАЖНО: llama-3.3-70b-versatile тоже
# в процессе отключения — не откатываться туда. Рекомендованное направление
# Groq — gpt-oss (text) / qwen3.6-27b (vision).
# Переопределяются через GROQ_*_MODEL в env — если Groq снова что-то снимет
# с продакшена, можно быстро подменить модель без деплоя кода.
TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-20b")
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")


def categorize_text(remainder_text: str, categories: list[str]) -> str:
    prompt = (
        f"Определи наиболее подходящую категорию из списка: {', '.join(categories)}.\n"
        f"Текст траты/дохода: \"{remainder_text}\"\n"
        f"Ответь ТОЛЬКО названием категории из списка, без пояснений."
    )
    completion = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=200,          # запас для reasoning-модели (gpt-oss)
        reasoning_effort="low",  # не нужны развёрнутые рассуждения на классификацию в одно слово
    )
    answer = (completion.choices[0].message.content or "").strip()
    logging.info(f"categorize_text: remainder={remainder_text!r} raw_answer={answer!r}")

    # Подстраховка: если модель вернула что-то не из списка — берём "Разное"
    for cat in categories:
        if cat.lower() in answer.lower():
            return cat
    return "Разное"


def narrate_period_comparison(data_summary: str) -> str:
    """Оборачивает УЖЕ ПОСЧИТАННЫЕ Python-ом числа (см. narrative_report.py)
    в связный текст на пару предложений. Модель здесь ничего не считает и
    не должна ничего досчитывать сама — только облекает в прозу то, что ей
    дали. Именно это правило один раз пришлось добавлять постфактум в
    другом проекте (Speech Flow Pro, generate_stats_deep_dive) — там без
    явного запрета модель придумывала иллюстративные примеры от себя,
    которые не соответствовали реальным данным пользователя. Здесь запрет
    сразу в промпте, а не патчем после того как кто-то заметит вымысел."""
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": (
                "Ты часть бота учёта семейных финансов. Тебе дают уже посчитанные "
                "числа — доходы/расходы за два периода и заметные изменения по "
                "категориям. Твоя задача — облечь их в короткий связный текст "
                "(2-4 предложения, не больше 350 знаков), не список и не заголовки. "
                "СТРОГО используй только переданные числа. Никогда не досчитывай, "
                "не округляй по-своему и не придумывай цифры, категории или причины "
                "изменений, которых нет в данных — если причина неизвестна, не "
                "гадай о ней. Тон нейтрально-наблюдательный, без нравоучений и без "
                "советов 'как сэкономить'. Пиши по-русски, обычным языком, не "
                "канцеляритом."
            )},
            {"role": "user", "content": data_summary},
        ],
        temperature=0.4,
        max_tokens=200,
    )
    return response.choices[0].message.content.strip()


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
        language="ru",
    )
    return transcription.text.strip()


def parse_question(text: str, categories: list[str], car_names: list[str]) -> dict | None:
    """Разбирает вопрос типа 'сколько я потратил на корм в июне?' в структуру:
    {"intent": "spending"|"income"|"mileage"|"last_date"|"quantity"|"unknown",
     "category": <строка из categories или null>,
     "item": <конкретный товар/продукт из вопроса, если назван, иначе null>,
     "car_name": <строка из car_names или null>,
     "period_type": "specific_month"|"current_period"|"all_time",
     "month": <1-12 или null>, "year": <год или null>}

    intent=last_date — вопросы "когда" ("когда я менял масло на опеле?",
    "когда последний раз покупал кофе?"): ищем не сумму, а дату САМОГО
    ПОСЛЕДНЕГО подходящего события за всю историю — period_type для этого
    intent'а по смыслу неважен (код в insights.py его игнорирует), но
    схема всё равно требует его заполнить.

    intent=quantity — вопросы про ФИЗИЧЕСКОЕ количество купленного, а не
    деньги ("сколько мороженого я купил?", "сколько литров масла купил в
    этом месяце?"). Отличай от intent=spending: "сколько Я ПОТРАТИЛ на Х"
    (деньги, spending) vs "сколько Х Я КУПИЛ" (штуки/кг/литры, quantity) —
    формально похожие вопросы на русском, но отвечают на них по-разному.

    "item" — для случаев вроде "сколько потратил на мороженое?": мороженое
    не входит в список категорий пользователя (только "Продукты" и т.п.),
    и раньше вопрос молча сводился к сумме по всей угаданной категории, а
    не по конкретному товару. Теперь LLM явно отделяет "товар" от
    "категория" — дальше наш код ищет "item" подстрокой в описании
    транзакции, а не в названии категории.

    НЕ используем response_format ни в каком виде — у gpt-oss на Groq была
    подтверждённая проблема с игнорированием json_schema (модель тихо
    возвращает свободный текст), и надёжность даже json_object под вопросом.
    Вместо этого — явная схема прямо в промпте + защитный разбор ответа.
    Возвращает None при любом сбое (сеть, парсинг) — вызывающий код должен
    вежливо ответить "не понял вопрос", а не падать.
    """
    schema_hint = (
        '{"intent": "spending" | "income" | "mileage" | "last_date" | "quantity" | "unknown", '
        '"category": "<строка из известных категорий или null>", '
        '"item": "<конкретный товар/продукт, если назван явно, иначе null>", '
        '"car_name": "<строка из известных машин или null>", '
        '"period_type": "specific_month" | "current_period" | "all_time", '
        '"month": <число 1-12 или null>, "year": <число или null>}'
    )
    known = (
        f"Известные категории: {', '.join(categories) if categories else 'нет'}.\n"
        f"Известные машины: {', '.join(car_names) if car_names else 'нет'}."
    )
    prompt = (
        f"Вопрос пользователя: \"{text}\"\n{known}\n\n"
        f"Разбери вопрос строго в JSON по этой схеме, без пояснений и markdown:\n{schema_hint}\n\n"
        "Правила:\n"
        "- intent=last_date, если спрашивают КОГДА произошло что-то в последний "
        "раз (\"когда я менял масло на опеле?\", \"когда последний раз покупал "
        "кофе?\", \"когда я заправлялся?\") — спрашивают дату/момент времени, "
        "а не сумму и не пробег.\n"
        "- intent=mileage только если явно спрашивают про пробег/километраж "
        "(и это НЕ вопрос \"когда\" — \"когда обновляли пробег\" всё ещё "
        "last_date, а не mileage).\n"
        "- intent=quantity, если спрашивают КОЛИЧЕСТВО купленного (в штуках/"
        "кг/литрах и т.п.), а НЕ потраченные деньги: \"сколько мороженого я "
        "купил?\", \"сколько литров масла купил в этом месяце?\". Если "
        "вопрос про потраченные деньги (\"сколько Я ПОТРАТИЛ на...\") — это "
        "intent=spending, а не quantity, даже если формулировка похожа.\n"
        "- intent=spending для трат/расходов, intent=income для доходов/зарплаты.\n"
        "- category — ТОЛЬКО точное совпадение из списка известных категорий, иначе null.\n"
        "- item — если вопрос про КОНКРЕТНУЮ вещь, которой нет в списке известных "
        "категорий: продукт (\"мороженое\", \"хлеб\", \"корм для кота\"), ИЛИ деталь/"
        "узел/жидкость/расходник автомобиля (\"рычаги\", \"тормозные колодки\", "
        "\"ремень ГРМ\", \"антифриз\", \"масло\", \"аккумулятор\", \"бензин\", "
        "\"топливо\", \"дизель\", \"солярка\") — положи её сюда в исходной форме "
        "из вопроса (падеж/число как в вопросе — не нормализуй), а category оставь "
        "null, если явно не совпадает ни с одной категорией. Если и товар/деталь, и "
        "категория упомянуты — заполни оба поля. Для вопросов про машину (car_name "
        "задан) item ОСОБЕННО ВАЖЕН: без него непонятно, ЧТО именно спрашивают про "
        "неё — \"когда меняли рычаги на матизе?\" ДОЛЖЕН дать item=\"рычаги\", "
        "\"когда покупал бензин на опель?\" ДОЛЖЕН дать item=\"бензин\", иначе "
        "вопрос без ответа по существу (найдётся любое последнее событие по машине, "
        "а не именно то, что спросили).\n"
        "- ГЛАГОЛ без явного существительного тоже подразумевает item, если "
        "однозначно называет тип траты: \"когда заправлял опель?\"/\"когда "
        "заправлялся?\" -> item=\"бензин\" (заправка = покупка топлива), даже "
        "если слово \"бензин\" в вопросе не произнесено напрямую.\n"
        "- car_name — ТОЛЬКО точное совпадение из списка известных машин, иначе null.\n"
        "- period_type=specific_month, если назван конкретный месяц (год может быть не назван).\n"
        "- period_type=current_period, если период не назван вообще (спрашивают "
        "\"сколько я потратил\" без уточнения когда).\n"
        "- period_type=all_time, если явно просят за всё время/всего/с начала.\n"
        "- month — номер месяца 1-12, если назван (иначе null). year — если назван явно (иначе null)."
    )
    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
            reasoning_effort="low",
        )
        answer = (completion.choices[0].message.content or "").strip()
    except Exception:
        logging.exception("parse_question: Groq request failed")
        return None

    logging.info(f"parse_question: text={text!r} raw_answer={answer!r}")

    # Защитный разбор: снимаем возможные markdown-обёртки ```json ... ```,
    # берём первый {...} блок, а не доверяем, что весь ответ — чистый JSON.
    cleaned = answer.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        logging.warning(f"parse_question: no JSON object found in answer: {answer!r}")
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logging.warning(f"parse_question: failed to decode JSON: {match.group(0)!r}")
        return None

    if not isinstance(parsed, dict) or "intent" not in parsed:
        return None
    return parsed


def extract_receipt_total(image_bytes: bytes) -> float | None:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "На фото чек из магазина или кафе. Найди итоговую сумму покупки "
        "(строка 'Итого' / 'К оплате' / 'Сумма'). "
        "Ответь СТРОГО в формате: СУММА: <число без валюты и пробелов>. "
        "Если не удаётся распознать сумму, ответь: СУММА: НЕТ"
    )
    completion = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                ],
            }
        ],
        temperature=0,
        max_tokens=60,
    )
    answer = (completion.choices[0].message.content or "").strip()
    logging.info(f"extract_receipt_total: raw_answer={answer!r}")

    match = re.search(r"(\d+(?:[.,]\d+)?)", answer)
    if not match or "НЕТ" in answer.upper():
        return None
    return float(match.group(1).replace(",", "."))
