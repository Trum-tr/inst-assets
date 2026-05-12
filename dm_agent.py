"""
DM Agent v2.0 — Instagrapi
===========================
Автоответчик для @inst.insider.ru.
Читает входящие DM, ловит триггеры ГАЙД / ОХВАТЫ / REELS,
отвечает с PDF-ссылкой и логирует лиды.

Запуск: python3 dm_agent.py
Работает в бесконечном цикле (poll каждые 60 сек).
"""

import os, json, time, re, subprocess, sys, signal, io
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dotenv import load_dotenv
from prompt_library import get_prompt
from lead_registry import add_lead, mark_followup_sent, get_leads_for_followup

# Unbuffered output — чтобы логи писались сразу (Mac + Windows)
try:
    sys.stdout = io.TextIOWrapper(open(sys.stdout.fileno(), 'wb', 0), encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(open(sys.stderr.fileno(), 'wb', 0), encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

for pkg in ["instagrapi", "requests"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg,
                        "-q"])

from instagrapi import Client
from instagrapi.exceptions import LoginRequired
import requests

# ── Конфиг ────────────────────────────────────────────────────────────────
IG_USERNAME   = os.getenv("IG_USERNAME", "inst.insider.ru")
IG_PASSWORD   = os.getenv("IG_PASSWORD")
TG_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")

SESSION_FILE  = Path(__file__).parent / "ig_session.json"
REPLIED_FILE  = Path(__file__).parent / "dm_replied.json"
LEADS_FILE    = Path(__file__).parent / "lead_registry.json"

POLL_INTERVAL = 60
MAX_THREADS   = 20

GD = "https://drive.google.com/uc?export=download&id="

# ── URL-ы для триггеров (редактируй здесь) ────────────────────────────────
TRIGGER_URLS = {
    # Awareness — бесплатные материалы
    "ГАЙД":          f"{GD}1l1hEE4VeyKn3_HeNUDOZdQcELy5Lxbo_",
    "ОХВАТЫ":        f"{GD}1T3aYe-ScCaJ4BkVK_TJQh9VFSCEmmG9O",
    "REELS":         f"{GD}1JPMZWn_V0EI564UjBYm0a0q9E3wlLi9b",
    "ПЛАН":          f"{GD}1l1hEE4VeyKn3_HeNUDOZdQcELy5Lxbo_",
    "КЕЙС":          f"{GD}1l1hEE4VeyKn3_HeNUDOZdQcELy5Lxbo_",
    # Consideration — без URL, личный диалог
    "СОВЕТ":         "",
    "СТРАТЕГИЯ":     "",
    "РАЗБОР":        "",
    # Decision — горячие лиды
    "КОНСУЛЬТАЦИЯ":  "",
    "ЦЕНА":          "",
    "СОТРУДНИЧЕСТВО":"",
}

# Классификация интентов по температуре лида
INTENT_TEMPERATURE = {
    "ГАЙД":          "cold",
    "ОХВАТЫ":        "cold",
    "REELS":         "cold",
    "ПЛАН":          "cold",
    "КЕЙС":          "warm",
    "СОВЕТ":         "warm",
    "СТРАТЕГИЯ":     "warm",
    "РАЗБОР":        "warm",
    "КОНСУЛЬТАЦИЯ":  "hot",
    "ЦЕНА":          "hot",
    "СОТРУДНИЧЕСТВО":"hot",
}

# Follow-up сообщения (отправляются через 24ч если нет ответа)
FOLLOWUP_MESSAGES = {
    "cold": (
        "Привет! 👋 Успел посмотреть материал?\n\n"
        "Если есть вопросы по своему аккаунту — пиши, разберём! 🔥"
    ),
    "warm": (
        "Привет! 👋 Как дела с аккаунтом?\n\n"
        "Если хочешь — могу сделать быстрый разбор: пришли ник и скажу "
        "что конкретно мешает расти 🎯"
    ),
    "hot": None,  # горячих не трогаем — они сами напишут
}


def _parse_dm_replies(raw: str) -> dict:
    """Парсит prompts/dm_reply.txt в dict {ТРИГГЕР: шаблон_ответа}."""
    replies = {}
    sections = [s.strip() for s in raw.split("---") if s.strip()]
    for section in sections:
        lines = section.splitlines()
        if not lines:
            continue
        header = lines[0].strip().rstrip(":")
        if header and header.isupper():
            body = "\n".join(lines[1:]).strip()
            replies[header] = body
    return replies


def build_triggers() -> dict:
    """Строит TRIGGERS: шаблоны из Prompt Library + URL из TRIGGER_URLS."""
    raw = get_prompt("dm_reply")
    reply_templates = _parse_dm_replies(raw)

    triggers = {}
    for name, url in TRIGGER_URLS.items():
        template = reply_templates.get(name, "Привет! 👋\n\nДержи материал:\n📎 {url}")
        triggers[name] = {"url": url, "reply": template}

    return triggers


TRIGGERS = build_triggers()

# ── Вспомогательные функции ───────────────────────────────────────────────
def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def send_followups(cl):
    """Отправляет follow-up сообщения лидам через 24 часа."""
    pending = get_leads_for_followup(hours=24)
    if not pending:
        return

    for lead in pending:
        temperature = lead.get("temperature", "cold")
        msg = FOLLOWUP_MESSAGES.get(temperature)
        if not msg:
            mark_followup_sent(lead["username"])
            continue
        try:
            username  = lead["username"]
            user_info = cl.user_info_by_username(username)
            threads   = cl.direct_threads(amount=50)
            target    = None
            for t in threads:
                for u in t.users:
                    if str(u.pk) == str(user_info.pk):
                        target = t
                        break
                if target:
                    break
            if target:
                cl.direct_send(msg, thread_ids=[target.id])
                mark_followup_sent(username)
                print(f"  Follow-up → @{username} [{temperature}]")
                send_telegram(
                    f"<b>Follow-up отправлен</b>\n"
                    f"@{username} | {lead.get('trigger','')} | {temperature}"
                )
                time.sleep(5)
        except Exception as e:
            print(f"  Follow-up ошибка @{lead.get('username','')}: {e}")


def reload_triggers():
    """Перечитывает шаблоны из Prompt Library без перезапуска агента."""
    global TRIGGERS
    try:
        TRIGGERS = build_triggers()
        print("  [PromptLibrary] Триггеры перезагружены")
    except Exception as e:
        print(f"  [PromptLibrary] Ошибка перезагрузки: {e}")


def detect_trigger(text):
    upper = text.upper()
    for kw in TRIGGERS:
        if re.search(r'\b' + kw + r'\b', upper):
            return kw
    return None

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"  Telegram: {e}")

def log_lead(username, trigger):
    temperature = INTENT_TEMPERATURE.get(trigger, "cold")
    add_lead(username, trigger, temperature)
    print(f"  Lead: @{username} [{trigger}] temperature={temperature}")

# ── Instagram Client ──────────────────────────────────────────────────────
LOGIN_TIMEOUT = 90  # секунд

def _do_login(cl, fresh=False):
    """Выполняется в отдельном потоке с таймаутом."""
    if not fresh and SESSION_FILE.exists():
        # Восстанавливаем из сессии БЕЗ нового логина в Instagram API
        cl.load_settings(SESSION_FILE)
        cl.get_timeline_feed()   # только проверяем что сессия жива
        return
    # Полный новый логин (только если нет сессии)
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)

def _backup_session():
    """Создаёт резервную копию сессии перед удалением."""
    if SESSION_FILE.exists():
        backup = SESSION_FILE.parent / f"ig_session_backup_{int(time.time())}.json"
        import shutil
        shutil.copy2(SESSION_FILE, backup)

def build_client():
    cl = Client()
    cl.delay_range = [2, 5]

    # Попытка с сохранённой сессией
    if SESSION_FILE.exists():
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_do_login, cl, False)
                fut.result(timeout=LOGIN_TIMEOUT)
            print("  Сессия восстановлена")
            return cl
        except FuturesTimeout:
            print(f"  Таймаут восстановления сессии ({LOGIN_TIMEOUT}с), перелогин...")
        except Exception as e:
            err = str(e)
            print(f"  Сессия устарела ({err[:100]}), перелогин...")
            # Если Instagram не находит аккаунт по username — не удаляем сессию,
            # это может быть временная ошибка API. Ждём и пробуем снова.
            if "can't find an account" in err.lower() or "unknown" in err.lower():
                print("  Временная ошибка Instagram API — сохраняю сессию, жду...")
                raise RuntimeError(f"Временная ошибка Instagram API: {err}")
        _backup_session()
        SESSION_FILE.unlink(missing_ok=True)
        cl = Client()
        cl.delay_range = [2, 5]

    # Свежий логин
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_do_login, cl, True)
            fut.result(timeout=LOGIN_TIMEOUT)
        print("  Новая сессия сохранена")
        return cl
    except FuturesTimeout:
        raise RuntimeError(f"Логин завис (>{LOGIN_TIMEOUT}с). Проверь интернет / 2FA.")
    except Exception as e:
        raise RuntimeError(f"Ошибка логина: {e}")

# ── Основной цикл ────────────────────────────────────────────────────────
def run():
    if not IG_PASSWORD:
        print("IG_PASSWORD не задан в .env")
        print("Добавь: IG_PASSWORD=твой_пароль")
        return

    print(f"DM Agent v2.1 | @{IG_USERNAME}")
    print("-" * 40)

    # Логин с повторными попытками
    cl = None
    for attempt in range(1, 4):
        try:
            print(f"  Логин, попытка {attempt}...")
            cl = build_client()
            break
        except RuntimeError as e:
            print(f"  {e}")
            if attempt < 3:
                wait = 60 * attempt
                print(f"  Жду {wait}с перед следующей попыткой...")
                time.sleep(wait)
    if cl is None:
        print("  Не удалось залогиниться за 3 попытки. Завершаю.")
        return

    replied = set(load_json(REPLIED_FILE, []))
    _tick = 0
    _RELOAD_EVERY = 30  # минут

    while True:
        try:
            # Горячая перезагрузка промптов каждые 30 минут
            if _tick % _RELOAD_EVERY == 0 and _tick > 0:
                reload_triggers()
            # ── Авто-одобрение запросов от не-подписчиков ────────────────
            try:
                pending = cl.direct_pending_inbox()
                for p in pending:
                    cl.direct_thread_approve(p.id)
                    sender = p.users[0].username if p.users else "?"
                    print(f"  Одобрен запрос от @{sender}")
                    time.sleep(1)
            except Exception as e:
                print(f"  pending inbox: {e}")

            # ── Обработка обычных тредов ──────────────────────────────────
            threads = cl.direct_threads(amount=MAX_THREADS)

            for thread in threads:
                if not thread.messages:
                    continue

                last_msg = thread.messages[0]
                msg_id   = str(last_msg.id)

                if msg_id in replied:
                    continue
                if str(last_msg.user_id) == str(cl.user_id):
                    replied.add(msg_id)
                    continue

                text    = last_msg.text or ""
                trigger = detect_trigger(text)

                if trigger:
                    info   = TRIGGERS[trigger]
                    reply  = info["reply"].format(url=info["url"])
                    sender = thread.users[0].username if thread.users else "unknown"

                    cl.direct_send(reply, thread_ids=[thread.id])
                    replied.add(msg_id)

                    log_lead(sender, trigger)
                    send_telegram(
                        f"<b>Новый лид!</b>\n"
                        f"@{sender}\n"
                        f"Триггер: <code>{trigger}</code>\n"
                        f"PDF отправлен"
                    )
                    print(f"  @{sender} -> {trigger} -> PDF отправлен")
                    time.sleep(3)

            save_json(REPLIED_FILE, list(replied))
            _tick += 1

            # Follow-up раз в час
            if _tick % 60 == 0:
                send_followups(cl)

            print(f"  [{datetime.now().strftime('%H:%M')}] OK, жду {POLL_INTERVAL}с...")

        except LoginRequired:
            print("  Сессия истекла, перелогин...")
            SESSION_FILE.unlink(missing_ok=True)
            try:
                cl = build_client()
            except RuntimeError as e:
                print(f"  {e} — жду 5 мин...")
                time.sleep(300)
        except Exception as e:
            print(f"  Ошибка: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
