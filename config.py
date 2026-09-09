import os
import auto_expense
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PORT = int(os.getenv("PORT", "8080"))

# Короткий кэш чтения листов Google Sheets (Redis). Сбрасывается при любой
# записи в тот же лист. 0 — выключить кэш (поведение как без него).
SHEETS_CACHE_TTL_SECONDS = int(os.getenv("SHEETS_CACHE_TTL_SECONDS", "60"))

# Таймаут HTTP-запросов к Google API (Sheets/Drive/OAuth). Без явного
# таймаута aiohttp по умолчанию ждёт до 5 минут на запрос — именно поэтому
# при кратковременной недоступности Google (503) пользователь видел ответ
# бота с задержкой ~3 минуты вместо быстрой ошибки или успешного повтора.
GOOGLE_HTTP_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_HTTP_TIMEOUT_SECONDS", "20"))

# Паузы между повторами при временных ошибках Google API (429/500/502/503/504)
# — они почти всегда самоустраняются за пару секунд. Первая попытка —
# без паузы, дальше нарастающая задержка. 4 записи = максимум 3 повтора.
GOOGLE_TRANSIENT_RETRY_DELAYS = [0, 1, 2, 4]

# Google OAuth — тот же Client ID/Secret, что уже настроен для PixKeep
# (Google Cloud проект + OAuth-клиент общие, это разные приложения на одном
# клиенте, не наоборот). Подтверждено: drive.file покрывает ВСЕ нужные
# Sheets API методы (create/batchUpdate/values.append/values.batchUpdate) —
# никакого Sensitive-скоупа "spreadsheets" не требуется.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Должен точно совпадать с Authorized redirect URI в Google Cloud Console,
# напр. https://<этот-render-домен>.onrender.com/oauth/callback — ОТДЕЛЬНЫЙ
# от PixKeep-бота URI, т.к. это другой Render-сервис с другим доменом.
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
GOOGLE_SCOPE = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/userinfo.email"
)

# Простой shared-secret для /cron/* эндпоинтов — без него кто угодно, кто
# найдёт URL, мог бы дёргать напоминания вручную сколько угодно раз.
CRON_SECRET = os.getenv("CRON_SECRET")

# Как часто изнутри бота (не через внешний cron) стучаться в Supabase, чтобы
# free-tier проект не приостановился из-за отсутствия активности (~7 дней
# без обращений к API). Дефолт — раз в 6 часов, большой запас на всякий случай.
SUPABASE_KEEPALIVE_INTERVAL_SECONDS = int(os.getenv("SUPABASE_KEEPALIVE_INTERVAL_SECONDS", str(6 * 60 * 60)))

# Ссылка на инструкцию (Telegraph). Можно поменять через переменную
# окружения без передеплоя кода, если гайд будет переопубликован по новому URL.
GUIDE_URL = os.getenv("GUIDE_URL", "https://telegra.ph/Instrukciya-k-Two-Pockets-bot-08-11")

# Дефолтные категории, создаются каждому юзеру при онбординге. Раньше была
# одна "Авто" на всё — теперь классификатор (auto_expense.classify_auto_type)
# сам решает, в какую из четырёх писать конкретную траты, см. forced_category
# ниже и changelog auto_expense.py.
DEFAULT_CATEGORIES = [
    "Продукты",
    "Транспорт",
    "Топливо",
    "Ремонт/ТО",
    "Запчасти",
    "Прочее",
    "Досуг",
    "Коммуналка",
    "Зарплата",
    "Подработка",
    "Разное",
]

# Ключевые слова -> тип транзакции (Доход), всё остальное = Расход
INCOME_KEYWORDS = [
    "зарплата", "зп", "получил", "получила", "кэшбэк", "кешбек",
    "возврат", "подработка", "премия", "аванс", "перевод от", "прода",
    "подарок от", "подарили",
]

# Символы валют, которые парсер распознаёт как признак суммы
CURRENCY_SYMBOLS = {
    "byn": "BYN", "br": "BYN",
    "р": "RUB", "руб": "RUB", "₽": "RUB",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
}

# Жёсткие правила категории — применяются РАНЬШЕ памяти (category_map) и LLM.
# Кортеж = все части должны встретиться в тексте (чтобы «ремонт» квартиры не
# стал «Авто»). Экономит поход к Groq на самых очевидных случаях и не зависит
# от того, правильно ли модель угадает в этот раз.
AUTO_FORCE_KEYWORDS = [
    "бензин", "дизел", "заправ", "антифриз", "тосол",
    "свеч", "фильтр масл", "масл мотор",
    ("ремонт", "авто"), ("ремонт", "машин"),
    ("то", "авто"), ("то", "машин"),
]
TRANSPORT_FORCE_KEYWORDS = [
    "такси", "яндекс го", "uber", "метро", "автобус", "трамвай",
    "маршрутка", "электричка", "проездн",
]


def keyword_hits(text: str, keywords: list) -> bool:
    lowered = f" {text.lower()} "
    for item in keywords:
        if isinstance(item, tuple):
            if all(part.lower() in lowered for part in item):
                return True
        elif item.lower() in lowered:
            return True
    return False


def forced_category(text: str) -> str | None:
    """None значит "не уверены, спроси LLM/юзера как обычно" — вызывающий
    код (input_handler.resolve_expense_category) просто продолжает свой
    обычный путь категоризации, если тут пусто.

    Для авто-ключевых слов возвращаем НЕ фиксированную строку, а результат
    auto_expense.classify_auto_type(text) — единый источник правды для
    того, какая из четырёх авто-категорий (Топливо/Ремонт-ТО/Запчасти/
    Прочее) это на самом деле. AUTO_FORCE_KEYWORDS ниже — просто более
    ранний, дешёвый способ понять "это точно про машину", сама категория
    внутри всё равно решается классификатором, а не этим списком."""
    if keyword_hits(text, AUTO_FORCE_KEYWORDS):
        return auto_expense.classify_auto_type(text)
    if keyword_hits(text, TRANSPORT_FORCE_KEYWORDS):
        return "Транспорт"
    return None


# Дешёвая эвристика ДО обращения к LLM/страховых веток парсера — вопросительный
# знак или явные вопросительные слова. Раньше жила локально в input_handler.py;
# перенесена сюда, чтобы tests_logic.py могла тестировать её без импорта
# aiogram-хендлеров, и чтобы не было двух копий одной и той же функции.
QUESTION_WORDS = ["сколько", "скольк", "какой", "какая", "какие", "какое", "когда", "где", "почему"]


def looks_like_question(text: str) -> bool:
    """Не идеально (не ловит вообще все формулировки), но и не должно: если
    промахнётся — сообщение просто попадёт в обычную "не вижу валюту" ветку,
    не страшно."""
    if "?" in text:
        return True
    lowered = f" {text.lower()} "
    return any(w in lowered for w in QUESTION_WORDS)




def redis_connection_kwargs() -> dict:
    """
    Устойчивая конфигурация соединения с Redis.

    Бесплатные managed-провайдеры (Upstash и т.п.) молча закрывают
    простаивающие TCP-соединения. Без этих настроек redis-py отдаёт из пула
    уже "протухшее" соединение и падает с ConnectionError на первом же
    сообщении после простоя — именно это происходило в проде
    ("Error UNKNOWN while writing to socket. Connection lost").

    - health_check_interval — перед использованием соединения, если оно
      давно простаивало, посылается PING; если сокет мёртв, соединение
      пересоздаётся ДО того, как в него полетит реальная команда.
    - retry / retry_on_error — если обрыв всё же произошёл, redis-py сам
      прозрачно повторит команду на новом соединении вместо того, чтобы
      уронить обработку апдейта у aiogram.

    Используется и для RedisStorage (main.py), и для отдельного клиента
    в google_oauth.py (хранение OAuth state) — оба должны быть одинаково
    устойчивы.
    """
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

    return {
        "health_check_interval": 30,
        "socket_keepalive": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "retry_on_timeout": True,
        "retry_on_error": [RedisConnectionError, RedisTimeoutError],
        "retry": Retry(ExponentialBackoff(base=0.5, cap=2.0), retries=3),
    }
