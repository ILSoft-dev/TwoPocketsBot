"""
Обёртка над Groq API:
- categorize_text  — LLM подбирает категорию из списка (fallback, если
  парсер по ключевым словам и category_map не справились)
- cluster_items    — группирует похожие описания трат в товары (для
  разбивки категории, insights._answer_breakdown)
- transcribe_voice  — Whisper large-v3, голос -> текст
- extract_receipt_total — Vision (qwen3.6-27b), фото чека -> сумма

Changelog:
- v1.8: parse_question принимает previous (разбор вопроса, заданного этим
        же пользователем < 10 мин назад) — уточняющие вопросы без повтора
        темы ("Сколько на сахар в июле?" -> "А в августе?"). Восстановленная
        фича — была в инструкции для пользователей, но кода не было вообще
        ни в каком виде; см. insights.py _get_previous_question/
        _remember_question.
- v1.7: два новых intent'а в parse_question — average (средний чек) и
        breakdown (разбивка категории на товары), плюс поле
        compare_previous (сравнение с прошлым периодом). Для breakdown
        добавлена cluster_items() — новый Groq-вызов, группирует уникальные
        описания трат в компактный набор товаров, суммы всё ещё считает
        код, не модель. См. insights.py _answer_average/_answer_breakdown/
        _answer_comparison.
- v1.6: parse_question получил intent=count ("сколько раз я покупал
        мороженое?") — считает число покупок/записей, отдельно от
        quantity (физическое количество) и spending (деньги). См.
        insights.py _answer_count.
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
import re
from groq import Groq

from config import GROQ_API_KEY

client = Groq(api_key=GROQ_API_KEY)

# llama-4-scout-17b-16e-instruct отключена Groq 17 июня 2026 (см.
# console.groq.com/docs/deprecations). ВАЖНО: llama-3.3-70b-versatile тоже
# в процессе отключения — не откатываться туда. Рекомендованное направление
# Groq — gpt-oss (text) / qwen3.6-27b (vision).
TEXT_MODEL = "openai/gpt-oss-20b"
VISION_MODEL = "qwen/qwen3.6-27b"
WHISPER_MODEL = "whisper-large-v3"


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


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
        language="ru",
    )
    return transcription.text.strip()


def parse_question(text: str, categories: list[str], car_names: list[str],
                   previous: dict | None = None) -> dict | None:
    """Разбирает вопрос типа 'сколько я потратил на корм в июне?' в структуру:
    {"intent": "spending"|"income"|"mileage"|"last_date"|"quantity"|"count"|"average"|"breakdown"|"unknown",
     "category": <строка из categories или null>,
     "item": <конкретный товар/продукт из вопроса, если назван, иначе null>,
     "car_name": <строка из car_names или null>,
     "period_type": "specific_month"|"current_period"|"all_time",
     "month": <1-12 или null>, "year": <год или null>,
     "compare_previous": true|false}

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

    intent=count — "сколько РАЗ" что-то покупалось ("сколько раз я покупал
    мороженое?", "сколько раз заправлялся в этом месяце?") — считает число
    ПОКУПОК/ЗАПИСЕЙ, а не деньги (spending) и не физическое количество
    (quantity, "3 мороженых" в одной записи всё равно считается как 1 раз).
    Ключевой маркер — слово "раз" рядом с "сколько".

    intent=average — средняя трата за покупку ("сколько я в среднем трачу
    на кофе?", "какой у меня средний чек в кафе?") — НЕ сумма за период
    (spending) и не количество (count/quantity), а сумма/число_покупок.

    intent=breakdown — "на что я больше всего трачу?" (разбивка КАТЕГОРИИ
    по товарам/статьям внутри неё, не по всем категориям сразу) —
    "на что я больше всего трачу в продуктах?", "на что уходят деньги в
    категории Досуг?". category для этого intent'а ОБЯЗАТЕЛЕН (это разбивка
    ОДНОЙ категории на составляющие, а не общий топ категорий) — если
    категория не названа явно, но понятно, что вопрос "внутри чего-то" —
    попробуй сопоставить с известными категориями по смыслу.

    compare_previous — true, если вопрос явно СРАВНИВАЕТ текущий период с
    предыдущим ("в этом месяце я трачу больше, чем в прошлом?", "насколько
    выросли траты на бензин по сравнению с прошлым месяцем?", "траты на
    бензин выросли?"). Работает только с intent=spending/income — период
    сравнения берётся автоматически (тот же промежуток, что и period_type,
    но на один шаг раньше). Во всех остальных случаях — false.

    "item" — для случаев вроде "сколько потратил на мороженое?": мороженое
    не входит в список категорий пользователя (только "Продукты" и т.п.),
    и раньше вопрос молча сводился к сумме по всей угаданной категории, а
    не по конкретному товару. Теперь LLM явно отделяет "товар" от
    "категория" — дальше наш код ищет "item" подстрокой в описании
    транзакции, а не в названии категории.

    previous — разбор ПРЕДЫДУЩЕГО вопроса этого же пользователя (если он
    был меньше 10 минут назад — см. insights._get_previous_question), для
    уточняющих вопросов без повтора темы ("Сколько на сахар в июле?" -> "А
    в августе?"). Формат: {"text": <текст предыдущего вопроса>,
    "parsed": <его разбор по этой же схеме>}. None, если контекста нет
    (первый вопрос, или прошло больше 10 минут).

    НЕ используем response_format ни в каком виде — у gpt-oss на Groq была
    подтверждённая проблема с игнорированием json_schema (модель тихо
    возвращает свободный текст), и надёжность даже json_object под вопросом.
    Вместо этого — явная схема прямо в промпте + защитный разбор ответа.
    Возвращает None при любом сбое (сеть, парсинг) — вызывающий код должен
    вежливо ответить "не понял вопрос", а не падать.
    """
    schema_hint = (
        '{"intent": "spending" | "income" | "mileage" | "last_date" | "quantity" | '
        '"count" | "average" | "breakdown" | "unknown", '
        '"category": "<строка из известных категорий или null>", '
        '"item": "<конкретный товар/продукт, если назван явно, иначе null>", '
        '"car_name": "<строка из известных машин или null>", '
        '"period_type": "specific_month" | "current_period" | "all_time", '
        '"month": <число 1-12 или null>, "year": <число или null>, '
        '"compare_previous": true | false}'
    )
    known = (
        f"Известные категории: {', '.join(categories) if categories else 'нет'}.\n"
        f"Известные машины: {', '.join(car_names) if car_names else 'нет'}."
    )
    context_block = ""
    if previous:
        context_block = (
            f"\nПредыдущий вопрос этого же пользователя (меньше 10 минут назад): "
            f"\"{previous.get('text', '')}\" -> {json.dumps(previous.get('parsed', {}), ensure_ascii=False)}\n"
        )
    prompt = (
        f"Вопрос пользователя: \"{text}\"\n{known}{context_block}\n\n"
        f"Разбери вопрос строго в JSON по этой схеме, без пояснений и markdown:\n{schema_hint}\n\n"
        "Правила:\n"
        + ("- Если новый вопрос — явное УТОЧНЕНИЕ предыдущего (не называет "
           "свою тему заново, например \"а в августе?\", \"а по продуктам?\", "
           "\"а сколько раз?\") — возьми недостающие поля (intent/category/"
           "item/car_name/period_type) из разбора предыдущего вопроса, а из "
           "нового текста — то, что явно меняется (обычно period_type/month/"
           "year, но может быть и intent, как в примере про \"сколько раз\"). "
           "Если новый вопрос самостоятельный и называет свою тему — "
           "полностью ИГНОРИРУЙ предыдущий контекст, разбирай с нуля.\n"
           if previous else "")
        + "- intent=last_date, если спрашивают КОГДА произошло что-то в последний "
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
        "- intent=count, если спрашивают СКОЛЬКО РАЗ что-то покупалось "
        "(\"сколько раз я покупал мороженое?\", \"сколько раз заправлялся в "
        "этом месяце?\") — это число покупок/записей, не деньги и не "
        "физическое количество внутри одной записи.\n"
        "- intent=average — средняя трата за покупку (\"сколько в среднем "
        "трачу на кофе?\", \"средний чек в кафе?\").\n"
        "- intent=breakdown — разбивка ОДНОЙ категории на товары/статьи "
        "внутри неё (\"на что я больше всего трачу в продуктах?\", \"на что "
        "уходят деньги в категории Досуг?\") — category обязателен, это НЕ "
        "топ категорий целиком, а разбор одной конкретной категории.\n"
        "- compare_previous=true, только если вопрос явно сравнивает с "
        "предыдущим периодом (\"больше, чем в прошлом месяце?\", \"выросли "
        "траты по сравнению с прошлым?\") — работает только с "
        "intent=spending/income, иначе всегда false.\n"
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


# Не отправляем в LLM бесконечно длинный список — уникальных описаний за
# один месяц у активного пользователя реалистично десятки, не тысячи;
# если вдруг больше — обрежем, чем сорвём промпт/ответ модели.
MAX_CLUSTER_ITEMS = 200


def cluster_items(comments: list[str]) -> dict[str, str] | None:
    """Группирует похожие по смыслу описания трат в компактный набор
    канонических названий товара — "молоко", "молоко 2.5%", "молочко" все
    получают одно и то же название группы. Используется для разбивки
    категории по товарам (insights._answer_breakdown, intent=breakdown):
    СУММЫ считает наш код по данным из Sheets, LLM только решает, какие
    строки описывают один и тот же товар — та же архитектура, что и во
    всех остальных вопросах (LLM разбирает текст, код считает цифры).

    Принимает список УНИКАЛЬНЫХ строк (дедуплицируй до вызова — незачем
    тратить токены на повторы). Возвращает {исходная_строка: название_группы}
    — каждый ключ входного списка обязательно попадает в результат (если
    модель забыла какую-то строку, она попадёт в свою же группу как есть,
    см. защитный разбор ниже). None при сбое сети/парсинга — вызывающий
    код должен сам решить фоллбэк (например, группировать 1:1 по буквальному
    тексту)."""
    if not comments:
        return {}

    truncated = comments[:MAX_CLUSTER_ITEMS]
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(truncated))
    prompt = (
        f"Вот список описаний трат из ОДНОЙ категории расходов:\n{numbered}\n\n"
        "Сгруппируй их по смыслу: одинаковые/похожие товары (разные словоформы, "
        "с уточнениями или без — \"молоко\"/\"молока\"/\"молоко 2.5%\") должны "
        "получить ОДНО общее короткое название на русском, в именительном "
        "падеже, единственном числе. Разные товары — разные названия, не "
        "объединяй без явного сходства по смыслу (не по случайному общему "
        "слову).\n\n"
        "Ответь строго в JSON-массиве, без пояснений и markdown, по одному "
        "объекту на каждую строку из списка выше:\n"
        '[{"i": <номер строки>, "group": "<название группы>"}, ...]'
    )
    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=max(300, len(truncated) * 12),
            reasoning_effort="low",
        )
        answer = (completion.choices[0].message.content or "").strip()
    except Exception:
        logging.exception("cluster_items: Groq request failed")
        return None

    logging.info(f"cluster_items: {len(truncated)} comments, raw_answer={answer[:500]!r}")

    cleaned = answer.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        logging.warning(f"cluster_items: no JSON array found in answer: {answer!r}")
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logging.warning(f"cluster_items: failed to decode JSON: {match.group(0)!r}")
        return None
    if not isinstance(parsed, list):
        return None

    grouping: dict[str, str] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("i")
        group = entry.get("group")
        if not isinstance(idx, int) or not isinstance(group, str) or not group.strip():
            continue
        if 1 <= idx <= len(truncated):
            grouping[truncated[idx - 1]] = group.strip()

    # Модель могла пропустить строку — подстрахуемся, чтобы КАЖДАЯ входная
    # строка гарантированно была в результате (своей же группой как есть),
    # иначе вызывающий код получит KeyError/молча потеряет данные.
    for c in truncated:
        grouping.setdefault(c, c)
    for c in comments[MAX_CLUSTER_ITEMS:]:
        grouping.setdefault(c, c)

    return grouping


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
