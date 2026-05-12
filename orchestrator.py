"""
Orchestrator v2.0 — Главный управляющий агент
===============================================
Координирует всю систему + управление через Telegram-бот.
Работает как daemon — запускается один раз и крутится 24/7.

Команды в Telegram:
  /run      — запустить полный цикл прямо сейчас
  /status   — состояние системы и KPI
  /leads    — статистика лидов
  /audit    — аудит системы
  /report   — полный отчёт + GPT-анализ
  /restart  — перезапустить оркестратор
  /help     — список команд

Запуск: python3 orchestrator.py
Логи:   orchestrator.log
"""

import os, sys, json, time, subprocess, logging, traceback, threading
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from prompt_library import get_prompt
from passport import passport_summary, apply_strategy_update
from lead_registry import add_lead, update_stage, get_funnel_stats, format_funnel_telegram

# ── Кросс-платформенная буферизация (Mac + Windows) ───────────────────────────
try:
    import io
    sys.stdout = io.TextIOWrapper(open(sys.stdout.fileno(), 'wb', 0), encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(open(sys.stderr.fileno(), 'wb', 0), encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

BASE     = Path(__file__).parent
LOG_FILE = BASE / "orchestrator.log"

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("orchestrator")

# ── Зависимости ───────────────────────────────────────────────────────────────
for pkg in ["requests", "openai", "instagrapi", "psutil"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg,
                        "-q"])

# ── Кросс-платформенное управление процессами ─────────────────────────────────
def find_procs_by_script(script_name: str) -> list:
    """Находит запущенные процессы Python по имени скрипта (Mac + Windows)."""
    try:
        import psutil
        result = []
        my_pid = os.getpid()
        for p in psutil.process_iter(['pid', 'cmdline']):
            try:
                if p.pid == my_pid:
                    continue
                cmdline = ' '.join(p.info['cmdline'] or [])
                if script_name in cmdline:
                    result.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return result
    except ImportError:
        return []

def kill_procs_by_script(script_name: str):
    """Завершает процессы по имени скрипта (Mac + Windows)."""
    for p in find_procs_by_script(script_name):
        try:
            p.terminate()
        except Exception:
            pass

import requests
from openai import OpenAI

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
IG_USER    = os.getenv("IG_USERNAME")
IG_PASS    = os.getenv("IG_PASSWORD")

# ── KPI-цели ─────────────────────────────────────────────────────────────────
KPI_TARGETS = {
    "followers":       10_000,
    "engagement_rate": 3.5,
    "weekly_leads":    20,
    "weekly_posts":    7,
    "account_value":   2_000,
}

STATE_FILE    = BASE / "orchestrator_state.json"
AUDIT_FILE    = BASE / "system_audit.json"
BLACKLIST_FILE= BASE / "topic_blacklist.json"
APPLIED_LOG   = BASE / "applied_actions.json"

PUBLISH_HOUR         = 9
ANALYTICS_HOUR       = 20
AUDIT_HOUR           = 3
OPTIMIZE_EVERY_HOURS = 6    # самооптимизация каждые 6 часов
STRATEGY_EVERY_DAYS  = 7    # стратегический анализ раз в 7 дней
RESEARCH_EVERY_DAYS  = 3    # исследование трендов раз в 3 дня

# ── Разрешённые автодействия ──────────────────────────────────────────────────
# Оркестратор может применять только эти действия автоматически.
# Всё остальное — только как рекомендация пользователю.
ALLOWED_ACTIONS = {
    "set_publish_hour":      "Изменить час публикации (0–23)",
    "set_content_count":     "Изменить количество постов в день (1–5)",
    "set_content_tone":      "Изменить тон контента (formal/casual/provocative)",
    "restart_dm_agent":      "Перезапустить DM-агента",
    "clear_content_cache":   "Очистить кеш сгенерированного контента",
    "reset_ig_session":      "Сбросить Instagram-сессию (потребует переавторизацию)",
    "add_topic_to_blacklist":"Добавить тему в стоп-лист контента",
    "set_optimize_interval": "Изменить интервал самооптимизации (часы)",
}

# ═══════════════════════════════════════════════════════════════════════════════
# ДВИЖОК ПРИМЕНЕНИЯ ДЕЙСТВИЙ
# ═══════════════════════════════════════════════════════════════════════════════

def log_applied_action(action: dict, result: str):
    """Сохраняет историю применённых действий."""
    record = {"date": datetime.now().isoformat(), "action": action, "result": result}
    history = []
    if APPLIED_LOG.exists():
        try:
            history = json.loads(APPLIED_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    history.append(record)
    APPLIED_LOG.write_text(json.dumps(history[-100:], ensure_ascii=False, indent=2), encoding="utf-8")

def patch_env(key: str, value: str):
    """Обновляет значение переменной в .env файле."""
    env_path = BASE / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines), encoding="utf-8")

def patch_file_line(filepath: Path, old_substr: str, new_line: str) -> bool:
    """Заменяет строку в Python-файле по подстроке."""
    if not filepath.exists():
        return False
    lines = filepath.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if old_substr in line:
            lines[i] = new_line
            filepath.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False

def apply_action(action: dict) -> tuple[bool, str]:
    """
    Применяет одно действие. Возвращает (успех, описание).
    action = {"type": "set_publish_hour", "value": 10, "reason": "..."}
    """
    global PUBLISH_HOUR, OPTIMIZE_EVERY_HOURS

    atype  = action.get("type", "")
    value  = action.get("value")
    reason = action.get("reason", "")

    if atype not in ALLOWED_ACTIONS:
        return False, f"Действие '{atype}' не разрешено"

    try:
        if atype == "set_publish_hour":
            hour = int(value)
            if not 0 <= hour <= 23:
                return False, "Час должен быть 0–23"
            PUBLISH_HOUR = hour
            patch_file_line(BASE / "orchestrator.py",
                            "PUBLISH_HOUR        =",
                            f"PUBLISH_HOUR        = {hour}")
            return True, f"Время публикации изменено на {hour}:00"

        elif atype == "set_content_count":
            count = int(value)
            if not 1 <= count <= 5:
                return False, "Количество постов должно быть 1–5"
            patch_env("CONTENT_COUNT", str(count))
            return True, f"Количество постов/день изменено на {count}"

        elif atype == "set_content_tone":
            tone_map = {
                "formal":      "профессиональный и авторитетный",
                "casual":      "разговорный, как друг",
                "provocative": "провокационный, задающий острые вопросы",
            }
            tone_ru = tone_map.get(str(value), str(value))
            patch_env("CONTENT_TONE", str(value))
            return True, f"Тон контента изменён: {tone_ru}"

        elif atype == "restart_dm_agent":
            kill_procs_by_script("dm_agent.py")
            time.sleep(2)
            subprocess.Popen(
                [sys.executable, str(BASE / "dm_agent.py")],
                cwd=str(BASE),
                stdout=open(BASE / "dm_agent.log", "a"),
                stderr=subprocess.STDOUT
            )
            return True, "DM-агент перезапущен"

        elif atype == "clear_content_cache":
            cf = BASE / "generated_content.json"
            if cf.exists():
                cf.unlink()
            return True, "Кеш контента очищен — следующий цикл сгенерирует свежий"

        elif atype == "reset_ig_session":
            # reset_ig_session ОТКЛЮЧЁН — удаление сессии ломает систему на VPS.
            # Сессия восстанавливается автоматически через load_settings().
            log.warning("reset_ig_session запрошен, но отключён — сессия защищена")
            return False, "reset_ig_session отключён: сессия нужна для работы с VPS"

        elif atype == "add_topic_to_blacklist":
            bl = []
            if BLACKLIST_FILE.exists():
                bl = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
            if str(value) not in bl:
                bl.append(str(value))
                BLACKLIST_FILE.write_text(json.dumps(bl, ensure_ascii=False, indent=2), encoding="utf-8")
            return True, f"Тема добавлена в стоп-лист: {value}"

        elif atype == "set_optimize_interval":
            hours = int(value)
            if not 1 <= hours <= 48:
                return False, "Интервал должен быть 1–48 часов"
            OPTIMIZE_EVERY_HOURS = hours
            patch_file_line(BASE / "orchestrator.py",
                            "OPTIMIZE_EVERY_HOURS =",
                            f"OPTIMIZE_EVERY_HOURS = {hours}   # самооптимизация каждые {hours} часов")
            return True, f"Интервал самооптимизации изменён на {hours}ч"

        return False, f"Действие {atype} не реализовано"

    except Exception as e:
        return False, f"Ошибка при выполнении {atype}: {e}"

def apply_actions_list(actions: list, notify_chat: str = None) -> list[str]:
    """Применяет список действий, возвращает отчёт."""
    report = []
    for action in actions:
        ok, msg = apply_action(action)
        icon = "✅" if ok else "❌"
        line = f"{icon} {ALLOWED_ACTIONS.get(action.get('type',''), action.get('type',''))}: {msg}"
        report.append(line)
        log_applied_action(action, msg)
        log.info(f"Применено действие {action.get('type')}: {msg}")
        if notify_chat:
            pass  # отчёт отправится единым блоком после
    return report

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — отправка и получение
# ═══════════════════════════════════════════════════════════════════════════════

def tg_send(text: str, chat_id: str = None, parse_mode="HTML", reply_markup: dict = None):
    cid = chat_id or TG_CHAT
    if not TG_TOKEN or not cid:
        return
    payload = {"chat_id": cid, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram send: {e}")

def tg_answer_callback(callback_id: str):
    """Подтверждаем нажатие кнопки (убирает индикатор загрузки)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=5
        )
    except Exception:
        pass


def register_bot_commands():
    """Регистрирует команды бота в Telegram-меню.
    Оставляем только то, что нельзя сделать кнопкой (требует текстовый аргумент).
    Всё остальное — через кнопки.
    """
    commands = [
        {"command": "addtopic",   "description": "Добавить тему: /addtopic Название | carousel | ГАЙД"},
        {"command": "cleartopic", "description": "Удалить тему из бэклога: /cleartopic ID"},
        {"command": "help",       "description": "Все команды и справка"},
    ]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands",
            json={"commands": commands},
            timeout=10
        )
        if r.json().get("ok"):
            log.info("Telegram-меню обновлено: 3 команды (addtopic, cleartopic, help)")
        else:
            log.warning(f"setMyCommands: {r.json()}")
    except Exception as e:
        log.warning(f"register_bot_commands: {e}")

def main_keyboard() -> dict:
    """Основная клавиатура с кнопками управления."""
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Дашборд",    "callback_data": "/dashboard"},
                {"text": "📈 Статус",     "callback_data": "/status"},
            ],
            [
                {"text": "🚀 Публикация", "callback_data": "/run"},
                {"text": "🎯 Лиды",       "callback_data": "/leads"},
            ],
            [
                {"text": "🔬 Диагностика", "callback_data": "/diagnose"},
                {"text": "🔄 Перезапуск",  "callback_data": "/start_agents"},
            ],
            [
                {"text": "🧠 Оптимизация", "callback_data": "/optimize"},
                {"text": "🔍 Аудит",       "callback_data": "/audit"},
            ],
            [
                {"text": "🔍 Ресёрч",      "callback_data": "/research"},
                {"text": "📊 Стратегия",   "callback_data": "/strategy"},
            ],
            [
                {"text": "📋 Бэклог",      "callback_data": "/backlog"},
                {"text": "🗺 Паспорт",     "callback_data": "/passport"},
            ],
            [
                {"text": "📋 Отчёт",        "callback_data": "/report"},
                {"text": "📊 Dashboard",    "callback_data": "/makedashboard"},
            ],
            [
                {"text": "🔥 Вирусный",     "callback_data": "/viral"},
                {"text": "🔥 Вирус (анализ)","callback_data": "/viraldry"},
            ],
        ]
    }

def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 30,
                    "allowed_updates": ["message", "callback_query"]},
            timeout=35
        )
        return r.json().get("result", [])
    except Exception:
        return []

# ── Отправить (псевдоним) ─────────────────────────────────────────────────────
send_telegram = tg_send

# ═══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "last_publish_date":   None,
        "last_analytics_date": None,
        "last_audit_date":     None,
        "publish_count":       0,
        "total_leads":         0,
        "followers":           0,
        "engagement_rate":     0.0,
        "strategy_notes":      [],
        "system_issues":       [],
    }

def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )

def run_script(script: str, args: list = None) -> tuple[bool, str]:
    cmd = [sys.executable, str(BASE / script)] + (args or [])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            cwd=str(BASE), encoding="utf-8", env=env
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Timeout (300 сек)"
    except Exception as e:
        return False, str(e)

def count_leads() -> int:
    f = BASE / "lead_registry.json"
    if not f.exists():
        return 0
    try:
        return len(json.loads(f.read_text(encoding="utf-8")))
    except Exception:
        return 0

def count_weekly_leads() -> int:
    f = BASE / "lead_registry.json"
    if not f.exists():
        return 0
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        return sum(1 for v in data.values()
                   if isinstance(v, dict) and v.get("date", "") >= week_ago)
    except Exception:
        return 0

def estimate_account_value(followers: int, er: float) -> int:
    if followers < 1000:
        return 0
    return int(followers / 1000 * 10 * max(0.5, er / 3.0))

def bar(pct: float, width=10) -> str:
    filled = min(int(pct / 100 * width), width)
    return "█" * filled + "░" * (width - filled)

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM-БОТ: КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_help(chat_id: str, _state: dict):
    tg_send(
        "<b>🤖 Оркестратор @inst.insider.ru</b>\n\n"
        "/dashboard  — полный дашборд KPI и системы\n"
        "/status     — краткое состояние и KPI\n"
        "/run        — запустить публикацию прямо сейчас\n"
        "/backlog    — очередь тем для публикации\n"
        "/addtopic   — добавить тему вручную\n"
        "/research   — запустить Research Agent\n"
        "/strategy   — запустить стратегический анализ\n"
        "/optimize   — анализ + GPT-оптимизация системы\n"
        "/report     — аналитика Instagram + GPT-совет\n"
        "/leads      — статистика лидов\n"
        "/audit      — аудит файлов и сессии\n"
        "/viral      — найти + репостить вирусный Reel с разбором\n"
        "/viraldry   — только анализ вирусного Reel (без публикации)\n"
        "/restart    — перезапустить оркестратор\n"
        "/help       — эта справка",
        chat_id
    )

def cmd_status(chat_id: str, state: dict):
    followers = state.get("followers", 0)
    er        = state.get("engagement_rate", 0.0)
    acc_val   = state.get("account_value", estimate_account_value(followers, er))
    pub_count = state.get("publish_count", 0)
    leads     = count_leads()
    w_leads   = count_weekly_leads()

    pct_f   = min(followers / KPI_TARGETS["followers"]   * 100, 100)
    pct_er  = min(er        / KPI_TARGETS["engagement_rate"] * 100, 100)
    pct_l   = min(w_leads   / KPI_TARGETS["weekly_leads"] * 100, 100)
    pct_v   = min(acc_val   / KPI_TARGETS["account_value"] * 100, 100)

    # Здоровье системы
    dm_log = BASE / "dm_agent.log"
    dm_ok  = dm_log.exists() and (datetime.now().timestamp() - dm_log.stat().st_mtime) < 300
    session_ok = (BASE / "ig_session.json").exists()

    tg_send(
        f"<b>📊 Статус системы</b>  {datetime.now().strftime('%d.%m %H:%M')}\n\n"
        f"<b>KPI:</b>\n"
        f"👥 Подписчики: {followers:,} / {KPI_TARGETS['followers']:,}\n"
        f"   {bar(pct_f)} {pct_f:.0f}%\n"
        f"💬 ER: {er}% / {KPI_TARGETS['engagement_rate']}%\n"
        f"   {bar(pct_er)} {pct_er:.0f}%\n"
        f"🎯 Лидов/нед: {w_leads} / {KPI_TARGETS['weekly_leads']}\n"
        f"   {bar(pct_l)} {pct_l:.0f}%\n"
        f"💰 Стоимость: ~${acc_val:,} / ${KPI_TARGETS['account_value']:,}\n"
        f"   {bar(pct_v)} {pct_v:.0f}%\n\n"
        f"<b>Система:</b>\n"
        f"{'✅' if dm_ok else '❌'} DM-бот\n"
        f"{'✅' if session_ok else '❌'} IG-сессия\n"
        f"📦 Постов опубликовано: {pub_count}\n"
        f"🎯 Всего лидов: {leads}",
        chat_id
    )

def cmd_leads(chat_id: str, _state: dict):
    try:
        stats = get_funnel_stats()
        if stats["total"] == 0:
            tg_send("Лидов пока нет. Дождись первых триггеров в директ.", chat_id)
            return
        tg_send(format_funnel_telegram(stats), chat_id)
    except Exception as e:
        tg_send(f"Ошибка: {e}", chat_id)


def cmd_updatelead(chat_id: str, _state: dict, text: str = ""):
    """Обновить статус лида: /updatelead @username stage [заметка]
    Этапы: new, dialogue, warm, client, lost
    """
    args = text.strip()
    if args.lower().startswith("/updatelead"):
        args = args[len("/updatelead"):].strip()

    parts = args.split(None, 2)
    if len(parts) < 2:
        tg_send(
            "Формат: <code>/updatelead @username stage</code>\n\n"
            "Этапы: new, dialogue, warm, client, lost\n\n"
            "Пример:\n"
            "<code>/updatelead @ivan_designer warm Хочет разбор</code>",
            chat_id
        )
        return

    username = parts[0].lstrip("@")
    stage    = parts[1].lower()
    note     = parts[2] if len(parts) > 2 else ""

    ok = update_stage(username, stage, note)
    if ok:
        labels = {"new":"🆕 Новый","dialogue":"💬 Диалог","warm":"🔥 Тёплый",
                  "client":"💰 Клиент","lost":"❌ Потерян"}
        tg_send(
            f"✅ @{username} → {labels.get(stage, stage)}"
            + (f"\n📝 {note}" if note else ""),
            chat_id
        )
    else:
        tg_send(f"❌ Лид @{username} не найден или этап '{stage}' неверный.", chat_id)


def cmd_makedashboard(chat_id: str, _state: dict):
    """Генерирует dashboard.html и сообщает путь."""
    try:
        from dashboard_generator import save_dashboard
        path = save_dashboard()
        tg_send(
            f"✅ <b>Dashboard обновлён</b>\n\n"
            f"📂 Открой в браузере:\n"
            f"<code>{path}</code>",
            chat_id
        )
    except Exception as e:
        tg_send(f"❌ Ошибка генерации: {e}", chat_id)

def cmd_run(chat_id: str, state: dict) -> dict:
    tg_send("🚀 Запускаю публикацию...\n⏳ Займёт 3-5 минут, результат придёт сюда.", chat_id)
    log.info("Telegram: ручной запуск /run")
    def _run():
        s = run_publish_cycle(state, notify_chat=chat_id)
        save_state(s)
    threading.Thread(target=_run, daemon=True).start()
    return state

def cmd_audit(chat_id: str, state: dict) -> dict:
    tg_send("🔍 Запускаю аудит...", chat_id)
    def _run():
        s = run_system_audit(state, notify_chat=chat_id)
        save_state(s)
    threading.Thread(target=_run, daemon=True).start()
    return state

def cmd_report(chat_id: str, state: dict) -> dict:
    tg_send("📈 Собираю отчёт + GPT-анализ...\n⏳ Займёт около минуты.", chat_id)
    def _run():
        s = run_analytics_cycle(state, notify_chat=chat_id)
        save_state(s)
    threading.Thread(target=_run, daemon=True).start()
    return state

def cmd_dashboard(chat_id: str, state: dict):
    """Отправляет полный дашборд прямо в Telegram."""
    followers = state.get("followers", 0)
    er        = state.get("engagement_rate", 0.0)
    acc_val   = state.get("account_value", estimate_account_value(followers, er))
    pub_count = state.get("publish_count", 0)
    leads     = count_leads()
    w_leads   = count_weekly_leads()

    pct_f  = min(followers / KPI_TARGETS["followers"]       * 100, 100)
    pct_er = min(er        / KPI_TARGETS["engagement_rate"] * 100, 100)
    pct_l  = min(w_leads   / KPI_TARGETS["weekly_leads"]    * 100, 100)
    pct_v  = min(acc_val   / KPI_TARGETS["account_value"]   * 100, 100)

    # Статус компонентов
    dm_ok      = (BASE / "dm_agent.log").exists() and \
                 (datetime.now().timestamp() - (BASE / "dm_agent.log").stat().st_mtime) < 600
    session_ok = (BASE / "ig_session.json").exists()
    content_ok = (BASE / "generated_content.json").exists()
    music_ok   = any((BASE / "music").glob("*.mp3")) if (BASE / "music").exists() else False

    # Следующие задачи
    now  = datetime.now()
    next_publish  = now.replace(hour=PUBLISH_HOUR,   minute=0, second=0)
    next_analytics= now.replace(hour=ANALYTICS_HOUR, minute=0, second=0)
    if next_publish   <= now: next_publish   += timedelta(days=1)
    if next_analytics <= now: next_analytics += timedelta(days=1)

    last_opt = state.get("last_optimize_time", "—")
    if last_opt != "—":
        try:
            last_opt = datetime.fromisoformat(last_opt).strftime("%d.%m %H:%M")
        except Exception:
            pass

    tg_send(
        f"<b>📊 ДАШБОРД @inst.insider.ru</b>\n"
        f"<i>{now.strftime('%d.%m.%Y %H:%M')}</i>\n"
        f"{'─' * 28}\n\n"

        f"<b>KPI — прогресс к целям</b>\n"
        f"👥 Подписчики:  {followers:,} / {KPI_TARGETS['followers']:,}\n"
        f"   {bar(pct_f)} {pct_f:.0f}%\n"
        f"💬 ER:          {er}% / {KPI_TARGETS['engagement_rate']}%\n"
        f"   {bar(pct_er)} {pct_er:.0f}%\n"
        f"🎯 Лидов/нед:  {w_leads} / {KPI_TARGETS['weekly_leads']}\n"
        f"   {bar(pct_l)} {pct_l:.0f}%\n"
        f"💰 Стоимость:  ~${acc_val:,} / ${KPI_TARGETS['account_value']:,}\n"
        f"   {bar(pct_v)} {pct_v:.0f}%\n\n"

        f"<b>Система</b>\n"
        f"{'✅' if dm_ok      else '❌'} DM-бот\n"
        f"{'✅' if session_ok else '❌'} Instagram-сессия\n"
        f"{'✅' if content_ok else '❌'} Контент\n"
        f"{'✅' if music_ok   else '⚠️'} Музыка\n\n"

        f"<b>Статистика</b>\n"
        f"📦 Постов опубликовано: {pub_count}\n"
        f"🎯 Всего лидов: {leads}\n\n"

        f"<b>Расписание</b>\n"
        f"🕘 Публикация: {next_publish.strftime('%d.%m %H:%M')}\n"
        f"🕗 Аналитика: {next_analytics.strftime('%d.%m %H:%M')}\n"
        f"🧠 Последняя оптимизация: {last_opt}\n\n"

        f"<b>Команды</b>\n"
        f"/run /audit /report /optimize /restart",
        chat_id
    )

def cmd_diagnose(chat_id: str, state: dict) -> dict:
    tg_send("🔬 Запускаю полную диагностику всех компонентов...", chat_id)
    state = run_auto_diagnostics(state, notify_chat=chat_id)
    save_state(state)
    return state

def cmd_research(chat_id: str, state: dict) -> dict:
    tg_send("🔍 Запускаю Research Agent...\n⏳ Анализ трендов займёт 3-5 минут.", chat_id)
    def _run():
        ok, out = run_script("research_agent.py")
        state["last_research_date"] = datetime.now().isoformat()
        save_state(state)
        if not ok:
            tg_send(f"⚠️ Research Agent ошибка:\n{out[-300:]}", chat_id)
    threading.Thread(target=_run, daemon=True).start()
    return state

def cmd_strategy(chat_id: str, state: dict) -> dict:
    tg_send("📊 Запускаю стратегический анализ...\n⏳ Займёт 2-3 минуты, результат придёт сюда.", chat_id)
    def _run():
        ok, out = run_script("strategy_agent.py")
        if not ok:
            tg_send(f"⚠️ Strategy Agent ошибка:\n{out[-300:]}", chat_id)
    threading.Thread(target=_run, daemon=True).start()
    state["last_strategy_date"] = datetime.now().isoformat()
    save_state(state)
    return state

def cmd_optimize(chat_id: str, state: dict) -> dict:
    tg_send("🧠 Запускаю анализ и оптимизацию системы...", chat_id)
    state = run_self_optimization(state, notify_chat=chat_id)
    save_state(state)
    return state

def cmd_passport(chat_id: str, _state: dict):
    """Показывает стратегический паспорт аккаунта."""
    tg_send(passport_summary(), chat_id)


def cmd_backlog(chat_id: str, _state: dict):
    """Показывает очередь тем из Content Backlog."""
    path = BASE / "content_backlog.json"
    if not path.exists():
        tg_send("📭 Content Backlog пуст. Запусти /research чтобы заполнить.", chat_id)
        return
    try:
        backlog = json.loads(path.read_text(encoding="utf-8"))
        pending = [x for x in backlog if x.get("status") == "pending"]
        used    = [x for x in backlog if x.get("status") == "used"]

        if not pending:
            tg_send(
                f"📭 Все темы использованы ({len(used)} шт.)\n"
                "Запусти /research чтобы добавить новые.", chat_id
            )
            return

        pending_sorted = sorted(pending, key=lambda x: -x.get("priority", 0))
        lines = []
        for i, item in enumerate(pending_sorted[:8], 1):
            fmt   = item.get("format", "carousel")
            prio  = item.get("priority", 0)
            emoji = "🖼️" if fmt == "carousel" else "🎬"
            lines.append(
                f"{i}. {emoji} <b>{item['topic'][:55]}</b>\n"
                f"   Триггер: {item.get('trigger_word','?')} | Приоритет: {prio}"
            )

        tg_send(
            f"<b>📋 Content Backlog</b>\n"
            f"В очереди: <b>{len(pending)}</b> | Использовано: {len(used)}\n"
            f"{'─'*28}\n\n"
            + "\n\n".join(lines) +
            (f"\n\n<i>...и ещё {len(pending)-8}</i>" if len(pending) > 8 else "") +
            "\n\n<i>Добавить тему: /addtopic Название темы</i>",
            chat_id
        )
    except Exception as e:
        tg_send(f"Ошибка чтения backlog: {e}", chat_id)


def cmd_addtopic(chat_id: str, _state: dict, text: str = ""):
    """Добавляет тему вручную в Content Backlog.
    Формат: /addtopic Название темы
    Или:    /addtopic Название | reels | ПЛАН
    """
    # Убираем команду, берём остаток
    args = text.strip()
    if text.lower().startswith("/addtopic"):
        args = text[len("/addtopic"):].strip()

    if not args:
        tg_send(
            "Напиши тему после команды:\n"
            "<code>/addtopic Как вырасти с 0 до 10к подписчиков</code>\n\n"
            "Или с параметрами (через |):\n"
            "<code>/addtopic Тема | carousel | ГАЙД</code>",
            chat_id
        )
        return

    # Парсим аргументы
    parts   = [p.strip() for p in args.split("|")]
    topic   = parts[0]
    fmt     = parts[1].lower() if len(parts) > 1 else "carousel"
    trigger = parts[2].upper() if len(parts) > 2 else "ГАЙД"

    if fmt not in ("carousel", "reels"):
        fmt = "carousel"

    path = BASE / "content_backlog.json"
    backlog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    # Генерируем ID
    existing_ids = [x.get("id", "") for x in backlog]
    new_id = f"manual-{len(backlog)+1:03d}"
    while new_id in existing_ids:
        new_id = f"manual-{int(new_id.split('-')[-1])+1:03d}"

    new_item = {
        "id":           new_id,
        "topic":        topic,
        "angle":        "ручное добавление",
        "pain":         "",
        "format":       fmt,
        "trigger_word": trigger,
        "priority":     7,
        "status":       "pending",
        "source":       "manual",
        "added_at":     datetime.now().isoformat(),
    }

    backlog.append(new_item)
    path.write_text(json.dumps(backlog, ensure_ascii=False, indent=2), encoding="utf-8")

    emoji = "🖼️" if fmt == "carousel" else "🎬"
    tg_send(
        f"✅ <b>Тема добавлена в очередь</b>\n\n"
        f"{emoji} {topic}\n"
        f"Формат: {fmt} | Триггер: {trigger}\n"
        f"ID: <code>{new_id}</code>\n\n"
        f"<i>Будет использована при следующей публикации.</i>",
        chat_id
    )


def cmd_cleartopic(chat_id: str, _state: dict, text: str = ""):
    """Удаляет тему из backlog по ID: /cleartopic manual-001"""
    args = text.strip()
    if text.lower().startswith("/cleartopic"):
        args = text[len("/cleartopic"):].strip()

    if not args:
        tg_send("Укажи ID темы: <code>/cleartopic manual-001</code>", chat_id)
        return

    path = BASE / "content_backlog.json"
    if not path.exists():
        tg_send("Backlog пуст.", chat_id)
        return

    backlog = json.loads(path.read_text(encoding="utf-8"))
    before  = len(backlog)
    backlog = [x for x in backlog if x.get("id") != args]

    if len(backlog) == before:
        tg_send(f"Тема с ID <code>{args}</code> не найдена.", chat_id)
        return

    path.write_text(json.dumps(backlog, ensure_ascii=False, indent=2), encoding="utf-8")
    tg_send(f"🗑️ Тема <code>{args}</code> удалена из backlog.", chat_id)


def cmd_restart(chat_id: str, _state: dict):
    tg_send(
        "♻️ <b>Перезапускаю оркестратор...</b>\n"
        "Через 5–10 секунд он снова выйдет на связь.",
        chat_id
    )
    log.info("Telegram: команда /restart — завершаю процесс")
    time.sleep(2)
    sys.exit(0)

def _viral_approval_keyboard() -> dict:
    """Кнопки подтверждения после анализа вирусного видео."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Опубликовать",  "callback_data": "/viral_publish"},
                {"text": "🔄 Следующий",     "callback_data": "/viral_next"},
            ],
            [
                {"text": "❌ Отмена",        "callback_data": "/viral_cancel"},
            ],
        ]
    }


def cmd_viral(chat_id: str, state: dict, text: str = "") -> dict:
    """Анализирует вирусное видео и показывает кнопки подтверждения.
    Использование: /viral @username
    Если username не указан — берёт первый из viral_accounts.json.
    """
    username = None
    for part in text.split():
        if part.startswith("@") and len(part) > 1:
            username = part.lstrip("@")
            break

    target_str = f"@{username}" if username else "первый из viral_accounts.json"
    if username:
        state["last_viral_username"] = username
    tg_send(
        f"🔥 <b>Viral Curator</b>\n"
        f"Ищу вирусное видео у {target_str}...\n"
        f"⏳ Займёт 1-2 минуты.",
        chat_id
    )

    def _run():
        args = [sys.executable, str(BASE / "viral_curator_agent.py")]
        if username:
            args.append(username)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"]       = "1"

        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=300,
                cwd=str(BASE), encoding="utf-8", env=env
            )
        except subprocess.TimeoutExpired:
            tg_send("⏰ Viral Curator: таймаут. Попробуй позже.", chat_id)
            return
        except Exception as e:
            tg_send(f"❌ Viral Curator: {e}", chat_id)
            return

        # Читаем результат анализа
        pending_file = BASE / "viral_pending.json"
        if not pending_file.exists():
            err = (result.stdout + result.stderr)[-300:]
            tg_send(f"❌ Анализ не удался:\n<code>{err}</code>", chat_id)
            return

        try:
            pending  = json.loads(pending_file.read_text(encoding="utf-8"))
        except Exception as e:
            tg_send(f"❌ Ошибка чтения pending: {e}", chat_id)
            return

        if "error" in pending:
            tg_send(f"⚠️ {pending['error']}", chat_id)
            return

        media    = pending.get("media", {})
        analysis = pending.get("analysis", {})
        views    = media.get("views", 0)
        likes    = media.get("likes", 0)
        user     = media.get("user", "?")

        def fv(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}М"
            if n >= 1_000:     return f"{n//1_000}к"
            return str(n) if n else "?"

        tg_send(
            f"<b>🔥 Найдено вирусное видео</b>\n\n"
            f"👤 Автор: @{user}\n"
            f"👁 Просмотры: {fv(views)}\n"
            f"❤️ Лайки: {fv(likes)}\n\n"
            f"<b>Почему вирусное:</b>\n{analysis.get('why_viral','')}\n\n"
            f"<b>Инсайты для разбора:</b>\n"
            f"1. {analysis.get('insight_1','')}\n"
            f"2. {analysis.get('insight_2','')}\n"
            f"3. {analysis.get('insight_3','')}\n\n"
            f"<b>Каптион:</b>\n<i>{analysis.get('repost_caption','')[:200]}...</i>\n\n"
            f"Публиковать это видео?",
            chat_id,
            reply_markup=_viral_approval_keyboard()
        )

    threading.Thread(target=_run, daemon=True).start()
    return state


def cmd_viral_publish(chat_id: str, state: dict) -> dict:
    """Публикует видео из viral_pending.json."""
    tg_send("⏳ Скачиваю видео, накладываю оверлей и публикую...\nЭто займёт 3-5 минут.", chat_id)

    def _run():
        args = [sys.executable, str(BASE / "viral_curator_agent.py"), "--publish-pending"]
        env  = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"]       = "1"
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=600,
                cwd=str(BASE), encoding="utf-8", env=env
            )
            out = result.stdout + result.stderr
            if result.returncode == 0 and "Опубликовано" in out:
                url = ""
                for line in out.splitlines():
                    if "instagram.com/reel/" in line:
                        url = line.strip().split()[-1]
                        break
                tg_send(
                    f"✅ <b>Reel опубликован!</b>\n"
                    + (f"<a href='{url}'>Открыть в Instagram</a>" if url else ""),
                    chat_id,
                    reply_markup=main_keyboard()
                )
            else:
                tg_send(f"❌ Ошибка публикации:\n<code>{out[-300:]}</code>", chat_id)
        except subprocess.TimeoutExpired:
            tg_send("⏰ Таймаут публикации (10 мин).", chat_id)
        except Exception as e:
            tg_send(f"❌ {e}", chat_id)

    threading.Thread(target=_run, daemon=True).start()
    return state


def cmd_viral_next(chat_id: str, state: dict) -> dict:
    """Пропускает текущее видео и ищет следующее."""
    # Добавляем текущее видео в обработанные, удаляем pending
    pending_file = BASE / "viral_pending.json"
    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            media_id = pending.get("media", {}).get("id")
            if media_id:
                processed_file = BASE / "viral_processed.json"
                processed = set()
                if processed_file.exists():
                    processed = set(json.loads(processed_file.read_text(encoding="utf-8")))
                processed.add(media_id)
                processed_file.write_text(json.dumps(sorted(processed), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        pending_file.unlink(missing_ok=True)

    # Берём username из предыдущего поиска или из viral_accounts.json
    username = state.get("last_viral_username")
    tg_send(
        f"🔄 Ищу следующее вирусное видео{f' у @{username}' if username else ''}...",
        chat_id
    )
    return cmd_viral(chat_id, state, text=f"@{username}" if username else "")


def cmd_viral_cancel(chat_id: str, state: dict) -> dict:
    """Отменяет текущий анализ."""
    (BASE / "viral_pending.json").unlink(missing_ok=True)
    tg_send("❌ Отменено. Видео не опубликовано.", chat_id, reply_markup=main_keyboard())
    return state


def cmd_viraldry(chat_id: str, state: dict, text: str = "") -> dict:
    """Viral Curator — только анализ без публикации (алиас для /viral)."""
    return cmd_viral(chat_id, state, text=text)


def cmd_start_agents(chat_id: str, _state: dict):
    """Перезапускает DM-агента и сообщает о статусе."""
    tg_send("🔄 Перезапускаю агентов...", chat_id)
    log.info("Telegram: /start_agents — перезапуск агентов")

    # Убиваем старый DM-агент
    kill_procs_by_script("dm_agent.py")
    time.sleep(2)

    # Запускаем заново
    dm_ok = start_dm_agent()
    time.sleep(3)

    # Проверяем
    dm_alive = bool(find_procs_by_script("dm_agent.py"))

    tg_send(
        f"<b>{'✅' if dm_alive else '❌'} Агенты перезапущены</b>\n\n"
        f"{'✅' if dm_alive else '❌'} DM-бот — {'работает' if dm_alive else 'ошибка запуска'}\n"
        f"✅ Оркестратор — работает\n\n"
        f"<i>Система готова к работе</i>",
        chat_id,
        reply_markup=main_keyboard()
    )

COMMANDS = {
    "/help":         cmd_help,
    "/status":       cmd_status,
    "/leads":        cmd_leads,
    "/run":          cmd_run,
    "/audit":        cmd_audit,
    "/report":       cmd_report,
    "/dashboard":    cmd_dashboard,
    "/optimize":     cmd_optimize,
    "/diagnose":     cmd_diagnose,
    "/research":     cmd_research,
    "/strategy":     cmd_strategy,
    "/passport":      cmd_passport,
    "/backlog":       cmd_backlog,
    "/updatelead":    cmd_updatelead,
    "/makedashboard": cmd_makedashboard,
    "/addtopic":     cmd_addtopic,
    "/cleartopic":   cmd_cleartopic,
    "/restart":      cmd_restart,
    "/start_agents": cmd_start_agents,
    "/viral":          cmd_viral,
    "/viraldry":       cmd_viraldry,
    "/viral_publish":  cmd_viral_publish,
    "/viral_next":     cmd_viral_next,
    "/viral_cancel":   cmd_viral_cancel,
}

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM POLLING THREAD
# ═══════════════════════════════════════════════════════════════════════════════

def handle_command(cmd: str, chat_id: str, state_ref: dict, full_text: str = ""):
    """Выполняет команду по имени. full_text — полное сообщение для команд с аргументами."""
    import inspect
    handler = COMMANDS.get(cmd)
    if handler:
        sig    = inspect.signature(handler)
        params = list(sig.parameters.keys())
        # Команды с аргументами (text=) получают полный текст сообщения
        if "text" in params:
            result = handler(chat_id, state_ref, text=full_text)
        elif len(params) >= 2:
            result = handler(chat_id, state_ref)
        else:
            result = handler(chat_id, state_ref)
        if isinstance(result, dict):
            state_ref.update(result)
    else:
        tg_send(f"Неизвестная команда: {cmd}\nНапиши /help", chat_id)

def telegram_bot_loop(state_ref: dict):
    """Слушает команды из Telegram в отдельном потоке."""
    log.info("🤖 Telegram-бот запущен — жду команды")

    # Сначала сбрасываем очередь старых сообщений — получаем все накопленные
    # updates с offset=-1 чтобы получить только последний update_id
    start_offset = 0
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": -1, "limit": 1},
            timeout=10
        )
        result = r.json().get("result", [])
        if result:
            start_offset = result[-1]["update_id"] + 1
            log.info(f"Пропущено старых сообщений, offset={start_offset}")
    except Exception:
        pass

    offset     = start_offset
    boot_time  = time.time()   # время запуска бота

    while True:
        try:
            updates = tg_get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1

                # ── Обычное текстовое сообщение ──────────────────────────
                if "message" in upd:
                    msg      = upd["message"]
                    msg_date = msg.get("date", 0)
                    # Игнорируем сообщения старше момента запуска
                    if msg_date < boot_time:
                        continue
                    text    = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if not text or not chat_id:
                        continue
                    if chat_id != str(TG_CHAT):
                        tg_send("⛔ Доступ запрещён.", chat_id)
                        continue
                    cmd = text.split()[0].lower()
                    log.info(f"Telegram команда: {cmd}")
                    handle_command(cmd, chat_id, state_ref, full_text=text)

                # ── Нажатие inline-кнопки ─────────────────────────────────
                elif "callback_query" in upd:
                    cb      = upd["callback_query"]
                    cb_id   = cb["id"]
                    cmd     = cb.get("data", "").strip()
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    tg_answer_callback(cb_id)
                    if chat_id != str(TG_CHAT):
                        continue
                    log.info(f"Telegram кнопка: {cmd}")
                    handle_command(cmd, chat_id, state_ref)

        except Exception as e:
            log.warning(f"Telegram polling error: {e}")
            time.sleep(5)

# ═══════════════════════════════════════════════════════════════════════════════
# ЦИКЛ 1: ПУБЛИКАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════════

def run_publish_cycle(state: dict, notify_chat: str = None) -> dict:
    log.info("═" * 50)
    log.info("🚀 ЦИКЛ ПУБЛИКАЦИИ — старт")
    chat = notify_chat or TG_CHAT

    steps = [
        ("content_agent.py",   [],          "📝 Генерация контента"),
        ("video_generator.py", [],          "🎬 Сборка видео"),
        ("instagram_agent.py", ["--auto"],  "📱 Публикация"),
    ]

    results = []
    all_ok  = True
    for script, args, label in steps:
        log.info(f"  → {label}")
        ok, output = run_script(script, args)
        results.append((label, ok))
        if not ok:
            all_ok = False
            log.error(f"  ✗ {output[-200:]}")
            if script == "content_agent.py":
                break
        else:
            log.info(f"  ✓ OK")

    state["last_publish_date"] = datetime.now().isoformat()
    state["publish_count"]     = state.get("publish_count", 0) + (1 if all_ok else 0)
    state["total_leads"]       = count_leads()

    status = "\n".join(f"{'✅' if ok else '❌'} {lbl}" for lbl, ok in results)
    w_leads = count_weekly_leads()

    tg_send(
        f"<b>{'✅' if all_ok else '⚠️'} Цикл публикации завершён</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"{status}\n\n"
        f"Лидов за неделю: <b>{w_leads}</b> / {KPI_TARGETS['weekly_leads']}",
        chat
    )
    log.info("🚀 ЦИКЛ ПУБЛИКАЦИИ — завершён")
    return state

# ═══════════════════════════════════════════════════════════════════════════════
# ЦИКЛ 2: АНАЛИТИКА
# ═══════════════════════════════════════════════════════════════════════════════

def pull_instagram_stats(state: dict) -> dict:
    try:
        from instagrapi import Client
        SESSION = BASE / "ig_session.json"
        cl = Client()
        cl.delay_range = [2, 4]
        if SESSION.exists():
            cl.load_settings(SESSION)
        cl.login(IG_USER, IG_PASS)
        cl.dump_settings(SESSION)

        user      = cl.user_info_by_username(IG_USER)
        followers = user.follower_count
        medias    = cl.user_medias(user.pk, amount=10)

        avg_er = 0.0
        if medias:
            total_er = sum(
                (m.like_count + m.comment_count) / max(followers, 1) * 100
                for m in medias
            )
            avg_er = round(total_er / len(medias), 2)

        state["followers"]       = followers
        state["engagement_rate"] = avg_er
        state["account_value"]   = estimate_account_value(followers, avg_er)
        log.info(f"  📊 {followers} подписчиков | ER {avg_er}% | ~${state['account_value']}")
    except Exception as e:
        log.warning(f"  ⚠️ Instagram stats: {e}")
    return state

def analyze_with_gpt(state: dict) -> str:
    if not OPENAI_KEY:
        return ""
    client  = OpenAI(api_key=OPENAI_KEY)
    w_leads = count_weekly_leads()
    prompt  = f"""Ты стратег Instagram-аккаунта @inst.insider.ru (ниша: раскрутка Instagram).
Цель: максимальная монетизация — продажи и продажа аккаунта.

МЕТРИКИ:
- Подписчики: {state.get('followers', 0)} (цель: {KPI_TARGETS['followers']})
- ER: {state.get('engagement_rate', 0)}% (цель: {KPI_TARGETS['engagement_rate']}%)
- Лидов/нед: {w_leads} (цель: {KPI_TARGETS['weekly_leads']})
- Стоимость: ~${state.get('account_value', 0)} (цель: ${KPI_TARGETS['account_value']})
- Постов: {state.get('publish_count', 0)}
- Системные проблемы: {state.get('system_issues', [])[-3:]}

Дай 3-5 КОНКРЕТНЫХ действий для роста прямо сейчас. Кратко, без воды."""
    try:
        r = OpenAI(api_key=OPENAI_KEY).chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500, temperature=0.7
        )
        return r.choices[0].message.content
    except Exception as e:
        log.warning(f"GPT: {e}")
        return ""

def run_analytics_cycle(state: dict, notify_chat: str = None) -> dict:
    log.info("═" * 50)
    log.info("📈 ЦИКЛ АНАЛИТИКИ — старт")
    chat = notify_chat or TG_CHAT

    state    = pull_instagram_stats(state)
    strategy = analyze_with_gpt(state)
    if strategy:
        notes = state.get("strategy_notes", [])
        notes.append({"date": datetime.now().isoformat(), "note": strategy})
        state["strategy_notes"] = notes[-10:]

    state["last_analytics_date"] = datetime.now().isoformat()

    followers = state.get("followers", 0)
    er        = state.get("engagement_rate", 0)
    acc_val   = state.get("account_value", 0)
    w_leads   = count_weekly_leads()

    pct = lambda v, t: min(v / t * 100, 100)

    tg_send(
        f"<b>📈 Аналитика @inst.insider.ru</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y')}\n\n"
        f"👥 {followers:,} подписч.  {bar(pct(followers, KPI_TARGETS['followers']))} {pct(followers, KPI_TARGETS['followers']):.0f}%\n"
        f"💬 ER {er}%  {bar(pct(er, KPI_TARGETS['engagement_rate']))} {pct(er, KPI_TARGETS['engagement_rate']):.0f}%\n"
        f"🎯 {w_leads} лидов/нед  {bar(pct(w_leads, KPI_TARGETS['weekly_leads']))} {pct(w_leads, KPI_TARGETS['weekly_leads']):.0f}%\n"
        f"💰 ~${acc_val:,}  {bar(pct(acc_val, KPI_TARGETS['account_value']))} {pct(acc_val, KPI_TARGETS['account_value']):.0f}%\n"
        + (f"\n<b>💡 GPT:</b>\n{strategy[:700]}" if strategy else ""),
        chat
    )
    log.info("📈 ЦИКЛ АНАЛИТИКИ — завершён")
    return state

# ═══════════════════════════════════════════════════════════════════════════════
# ЦИКЛ 3: АУДИТ
# ═══════════════════════════════════════════════════════════════════════════════

def run_system_audit(state: dict, notify_chat: str = None) -> dict:
    log.info("═" * 50)
    log.info("🔍 АУДИТ СИСТЕМЫ — старт")
    chat   = notify_chat or TG_CHAT
    issues, warnings, ok_list = [], [], []

    checks = {
        "content_agent.py":    "Генератор контента",
        "carousel_generator.py": "Генератор слайдов",
        "video_generator.py":  "Генератор видео",
        "instagram_agent.py":  "Публикатор",
        "dm_agent.py":         "DM-агент",
        "orchestrator.py":     "Оркестратор",
        ".env":                "Конфиг",
    }
    for fname, label in checks.items():
        if not (BASE / fname).exists():
            issues.append(f"Отсутствует: {fname}")
        else:
            ok_list.append(f"✓ {label}")

    # Музыка
    music = BASE / "music"
    tracks = [f for f in music.glob("*.mp3") if f.stat().st_size > 1000] if music.exists() else []
    if not tracks:
        warnings.append("Нет музыки — видео без звука")
    else:
        ok_list.append(f"✓ Музыка: {len(tracks)} треков")

    # DM-бот
    dm_log = BASE / "dm_agent.log"
    if dm_log.exists():
        age = (datetime.now().timestamp() - dm_log.stat().st_mtime) / 60
        if age > 5:
            warnings.append(f"DM-бот не активен {int(age)} мин")
        else:
            ok_list.append(f"✓ DM-бот активен")
    else:
        warnings.append("DM-агент не запускался")

    # IG-сессия
    session = BASE / "ig_session.json"
    if not session.exists():
        warnings.append("IG-сессия отсутствует")
    else:
        age_h = (datetime.now().timestamp() - session.stat().st_mtime) / 3600
        if age_h > 168:
            warnings.append(f"IG-сессия устарела ({int(age_h)}ч)")
        else:
            ok_list.append(f"✓ IG-сессия ({int(age_h)}ч)")

    # Контент
    cf = BASE / "generated_content.json"
    if cf.exists():
        age_h = (datetime.now().timestamp() - cf.stat().st_mtime) / 3600
        if age_h > 26:
            warnings.append(f"Контент не обновлялся {int(age_h)}ч")
        else:
            ok_list.append(f"✓ Контент свежий")
    else:
        issues.append("generated_content.json отсутствует")

    # Диск
    import shutil
    free_gb = shutil.disk_usage(BASE).free / (1024**3)
    if free_gb < 1.0:
        issues.append(f"Мало места: {free_gb:.1f} GB")
    else:
        ok_list.append(f"✓ Диск: {free_gb:.1f} GB свободно")

    audit = {"date": datetime.now().isoformat(),
             "issues": issues, "warnings": warnings, "ok": ok_list}
    AUDIT_FILE.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    state["system_issues"]   = issues + warnings
    state["last_audit_date"] = datetime.now().isoformat()

    # Отчёт
    lines = ""
    if issues:
        lines += "\n".join(f"❌ {i}" for i in issues) + "\n"
    if warnings:
        lines += "\n".join(f"⚠️ {w}" for w in warnings) + "\n"
    lines += "\n".join(ok_list)

    tg_send(
        f"<b>🔍 Аудит системы</b>  {datetime.now().strftime('%d.%m %H:%M')}\n\n"
        f"{'❌ Проблемы: ' + str(len(issues)) if issues else '✅ Критических проблем нет'}\n"
        f"{'⚠️ Предупреждений: ' + str(len(warnings)) if warnings else ''}\n\n"
        f"{lines}",
        chat
    )
    log.info("🔍 АУДИТ — завершён")
    return state

# ═══════════════════════════════════════════════════════════════════════════════
# ЦИКЛ 3б: АВТОДИАГНОСТИКА
# ═══════════════════════════════════════════════════════════════════════════════

def run_auto_diagnostics(state: dict, notify_chat: str = None) -> dict:
    """
    Полная проверка всех компонентов системы с реальными API-запросами:
    .env ключи → ig_session → Instagram API → OpenAI → Telegram → файлы → пакеты.
    Запускается при старте и каждые 60 минут.
    """
    log.info("🔬 АВТОДИАГНОСТИКА — старт")
    chat   = notify_chat or TG_CHAT
    issues = []
    ok_list= []

    # 1. Ключи в .env
    required_keys = ["IG_USERNAME", "IG_PASSWORD", "TELEGRAM_TOKEN",
                     "TELEGRAM_CHAT_ID", "OPENAI_API_KEY"]
    for k in required_keys:
        if not os.getenv(k):
            issues.append(f".env: отсутствует {k}")
        else:
            ok_list.append(f"{k}")

    # 2. ig_session.json — наличие и cookies
    session = BASE / "ig_session.json"
    if not session.exists():
        issues.append("ig_session.json не найден")
    else:
        try:
            data = json.loads(session.read_text(encoding="utf-8"))
            has_cookies = bool(data.get("cookies") or data.get("authorization_data"))
            has_uuids   = bool(data.get("uuids"))
            if not has_cookies:
                issues.append("ig_session.json: нет cookies — сессия неполная!")
            elif not has_uuids:
                issues.append("ig_session.json: нет uuids — сессия может не работать")
            else:
                ok_list.append("ig_session.json (cookies + uuids OK)")
        except Exception as e:
            issues.append(f"ig_session.json: ошибка чтения — {e}")

    # 3. Instagram сессия — реальный запрос
    try:
        from instagrapi import Client as IgClient
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FT
        _cl = IgClient()
        _cl.delay_range = [1, 2]
        def _ig_test():
            _cl.load_settings(BASE / "ig_session.json")
            _cl.get_timeline_feed()
            return True
        if session.exists():
            with ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(_ig_test).result(timeout=30)
            ok_list.append("Instagram сессия активна")
        else:
            issues.append("Instagram: сессия не найдена — пропуск теста")
    except Exception as e:
        err = str(e)[:120]
        if "timeout" in err.lower() or "FT" in str(type(e)):
            issues.append("Instagram: таймаут 30с — сервер недоступен?")
        else:
            issues.append(f"Instagram сессия: {err}")

    # 4. OpenAI
    try:
        from openai import OpenAI as _OAI
        _OAI(api_key=os.getenv("OPENAI_API_KEY")).chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=3
        )
        ok_list.append("OpenAI API")
    except Exception as e:
        issues.append(f"OpenAI: {str(e)[:100]}")

    # 5. Telegram
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=10
        ).json()
        if r.get("ok"):
            ok_list.append(f"Telegram @{r['result'].get('username')}")
        else:
            issues.append(f"Telegram: {r.get('description','TOKEN неверный')}")
    except Exception as e:
        issues.append(f"Telegram: {e}")

    # 6. Файлы системы
    required_files = [
        "dm_agent.py", "content_agent.py", "instagram_agent.py",
        "carousel_generator.py", "video_generator.py", ".env"
    ]
    missing_files = [f for f in required_files if not (BASE / f).exists()]
    if missing_files:
        issues.append(f"Файлы отсутствуют: {', '.join(missing_files)}")
    else:
        ok_list.append(f"Все файлы на месте ({len(required_files)})")

    # 7. Python пакеты
    missing_pkgs = []
    for pkg in ["instagrapi", "requests", "openai", "psutil"]:
        try:
            __import__(pkg)
        except ImportError:
            missing_pkgs.append(pkg)
    if missing_pkgs:
        issues.append(f"Пакеты не установлены: {', '.join(missing_pkgs)}")
    else:
        ok_list.append("Все пакеты установлены")

    # Сохраняем результат в state
    state["last_diagnostics"] = {
        "date":      datetime.now().isoformat(),
        "issues":    issues,
        "ok_count":  len(ok_list),
    }

    # Telegram-отчёт
    icon = "✅" if not issues else "❌"
    msg  = f"<b>{icon} Автодиагностика системы</b>  {datetime.now().strftime('%d.%m %H:%M')}\n\n"
    if issues:
        msg += "<b>Проблемы:</b>\n" + "\n".join(f"❌ {i}" for i in issues) + "\n\n"
    msg += f"<b>OK ({len(ok_list)}):</b> " + " · ".join(ok_list)
    if not issues:
        msg += "\n\n✅ Все компоненты работают нормально."

    tg_send(msg, chat)
    log.info(f"🔬 ДИАГНОСТИКА завершена. Проблем: {len(issues)}, OK: {len(ok_list)}")
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# ЦИКЛ 4: САМООПТИМИЗАЦИЯ СИСТЕМЫ
# ═══════════════════════════════════════════════════════════════════════════════

def read_log_tail(path: Path, lines=150) -> str:
    """Читает последние N строк лог-файла."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""

def count_log_errors(log_text: str) -> dict:
    """Считает частоту ошибок в логе."""
    counts = {"ERROR": 0, "WARNING": 0, "Timeout": 0, "Instagram": 0, "OpenAI": 0}
    for line in log_text.splitlines():
        for key in counts:
            if key.lower() in line.lower():
                counts[key] += 1
    return counts

def run_self_optimization(state: dict, notify_chat: str = None) -> dict:
    """
    Анализирует работу всей системы агентов:
    - читает логи на предмет ошибок и паттернов
    - проверяет целостность процессов
    - сравнивает KPI-тренды
    - запрашивает GPT-рекомендации
    - авто-исправляет простые проблемы
    """
    log.info("═" * 50)
    log.info("🧠 САМООПТИМИЗАЦИЯ — старт")
    chat  = notify_chat or TG_CHAT
    fixes = []
    warns = []

    # ── 1. Анализ логов ───────────────────────────────────────────────────────
    orch_log  = read_log_tail(LOG_FILE, 150)
    dm_log    = read_log_tail(BASE / "dm_agent.log", 50)
    err_log   = read_log_tail(BASE / "orchestrator_error.log", 50)
    errors    = count_log_errors(orch_log + err_log)

    if errors["ERROR"] > 10:
        warns.append(f"Много ошибок в логе: {errors['ERROR']} за последний период")
    if errors["Timeout"] > 3:
        warns.append(f"Частые таймауты ({errors['Timeout']}) — возможно нестабильный интернет")
    if errors["Instagram"] > 5:
        warns.append(f"Проблемы с Instagram API ({errors['Instagram']} ошибок) — сессия может устареть")

    # ── 2. Проверка целостности файлов системы ────────────────────────────────
    required = [
        "content_agent.py", "carousel_generator.py", "video_generator.py",
        "instagram_agent.py", "dm_agent.py", ".env"
    ]
    missing = [f for f in required if not (BASE / f).exists()]
    if missing:
        warns.append(f"Отсутствуют файлы: {', '.join(missing)}")

    # ── 3. Проверка свежести контента ─────────────────────────────────────────
    cf = BASE / "generated_content.json"
    if cf.exists():
        age_h = (datetime.now().timestamp() - cf.stat().st_mtime) / 3600
        if age_h > 30:
            warns.append(f"Контент не обновлялся {int(age_h)}ч — пропущена публикация?")

    # ── 4. Проверка IG-сессии ─────────────────────────────────────────────────
    session = BASE / "ig_session.json"
    if session.exists():
        age_h = (datetime.now().timestamp() - session.stat().st_mtime) / 3600
        if age_h > 120:
            warns.append(f"IG-сессия не обновлялась {int(age_h)}ч — может потребовать повторного входа")

    # ── 5. Авто-чистка: удаляем старые temp-файлы ────────────────────────────
    cleaned = 0
    for tmp in BASE.glob("instagram_posts/**/temp_*.aac"):
        try:
            tmp.unlink()
            cleaned += 1
        except Exception:
            pass
    if cleaned:
        fixes.append(f"Удалено {cleaned} временных аудио-файлов")

    # ── 6. Проверка места на диске ────────────────────────────────────────────
    import shutil
    free_gb = shutil.disk_usage(BASE).free / (1024 ** 3)
    if free_gb < 2.0:
        warns.append(f"Мало места: {free_gb:.1f} GB — старые посты займут много места")

    # ── 7. KPI-тренды ─────────────────────────────────────────────────────────
    prev_followers = state.get("prev_followers", 0)
    curr_followers = state.get("followers", 0)
    follower_delta = curr_followers - prev_followers
    state["prev_followers"] = curr_followers

    trend_note = ""
    if curr_followers > 0:
        if follower_delta > 0:
            trend_note = f"Прирост подписчиков: +{follower_delta}"
        elif follower_delta < 0:
            warns.append(f"Потеря подписчиков: {follower_delta} — проверь контент-стратегию")

    # ── 8. GPT-анализ + структурированные действия ───────────────────────────
    gpt_recs    = ""
    auto_applied= []
    manual_recs = []

    if OPENAI_KEY:
        try:
            client = OpenAI(api_key=OPENAI_KEY)
            actions_desc = "\n".join(
                f'  "{k}": {v}' for k, v in ALLOWED_ACTIONS.items()
            )
            user_data = f"""СОСТОЯНИЕ:
- Подписчики: {curr_followers} (изменение: {follower_delta:+d})
- ER: {state.get('engagement_rate', 0)}%
- Лидов/нед: {count_weekly_leads()} из {KPI_TARGETS['weekly_leads']}
- Публикаций: {state.get('publish_count', 0)}
- Стоимость: ~${state.get('account_value', 0)}

ВЫЯВЛЕННЫЕ ПРОБЛЕМЫ:
{chr(10).join(warns) if warns else 'Критических проблем нет'}

ОШИБКИ В ЛОГАХ:
- ERROR: {errors['ERROR']}, Timeout: {errors['Timeout']}, Instagram: {errors['Instagram']}

ПОСЛЕДНИЕ СТРОКИ ЛОГА:
{orch_log[-600:]}

ДОСТУПНЫЕ АВТОДЕЙСТВИЯ (применяются автоматически):
{actions_desc}"""

            r = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": get_prompt("optimization")},
                    {"role": "user",   "content": user_data},
                ],
                max_tokens=800, temperature=0.3
            )
            raw = r.choices[0].message.content.strip()
            # Чистим markdown если GPT обернул
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)

            auto_actions = parsed.get("auto_actions", [])
            manual_recs  = parsed.get("manual_recommendations", [])

            # Применяем автодействия
            if auto_actions:
                log.info(f"GPT предлагает {len(auto_actions)} автодействий")
                auto_applied = apply_actions_list(auto_actions)

            gpt_recs = "\n".join(manual_recs) if manual_recs else ""

        except json.JSONDecodeError as e:
            log.warning(f"GPT вернул невалидный JSON: {e}")
            gpt_recs = "GPT вернул невалидный ответ — пропускаю автоприменение"
        except Exception as e:
            log.warning(f"GPT оптимизация: {e}")

    # ── 9. Сохраняем результат ────────────────────────────────────────────────
    opt_record = {
        "date":         datetime.now().isoformat(),
        "warns":        warns,
        "fixes":        fixes,
        "auto_applied": auto_applied,
        "manual_recs":  manual_recs,
        "errors":       errors,
    }
    history = state.get("optimization_history", [])
    history.append(opt_record)
    state["optimization_history"] = history[-20:]
    state["last_optimize_time"]   = datetime.now().isoformat()

    # ── 10. Telegram-отчёт ────────────────────────────────────────────────────
    has_problems = bool(warns or auto_applied or gpt_recs)
    icon = "✅" if not warns else ("⚠️" if len(warns) < 3 else "❌")
    msg  = f"<b>{icon} Самооптимизация системы</b>  {datetime.now().strftime('%d.%m %H:%M')}\n\n"

    if fixes:
        msg += "<b>🔧 Системная чистка:</b>\n" + "\n".join(f"  {f}" for f in fixes) + "\n\n"

    if auto_applied:
        msg += "<b>⚡ Применено автоматически:</b>\n" + "\n".join(f"  {a}" for a in auto_applied) + "\n\n"

    if warns:
        msg += "<b>⚠️ Обнаружено:</b>\n" + "\n".join(f"  {w}" for w in warns) + "\n\n"

    if trend_note:
        msg += f"📈 {trend_note}\n\n"

    if gpt_recs:
        msg += f"<b>💡 Требует вашего решения:</b>\n{gpt_recs[:600]}"

    if not has_problems and not fixes:
        msg += "Система работает штатно. Нарушений не обнаружено."

    tg_send(msg, chat)
    log.info(f"🧠 САМООПТИМИЗАЦИЯ — завершена. Предупреждений: {len(warns)}, исправлений: {len(fixes)}")
    return state

# ═══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════════════════════════════════════════

def already_done_today(state: dict, key: str) -> bool:
    ds = state.get(key)
    if not ds:
        return False
    try:
        return datetime.fromisoformat(ds).date() == datetime.now().date()
    except Exception:
        return False

def done_within_hours(state: dict, key: str, hours: int) -> bool:
    """Возвращает True если задача выполнялась менее N часов назад."""
    ds = state.get(key)
    if not ds:
        return False
    try:
        last = datetime.fromisoformat(ds)
        return (datetime.now() - last).total_seconds() < hours * 3600
    except Exception:
        return False

def start_dm_agent() -> bool:
    """Запускает DM-агента как дочерний процесс если он не работает."""
    dm_script = BASE / "dm_agent.py"
    dm_log    = BASE / "dm_agent.log"

    if not dm_script.exists():
        log.warning("dm_agent.py не найден")
        return False

    # Проверяем жив ли уже процесс
    if find_procs_by_script("dm_agent.py"):
        log.info("DM-агент уже запущен")
        return True

    # Запускаем
    try:
        with open(dm_log, "a") as flog:
            proc = subprocess.Popen(
                [sys.executable, str(dm_script)],
                cwd=str(BASE),
                stdout=flog,
                stderr=flog
            )
        log.info(f"DM-агент запущен, PID: {proc.pid}")
        return True
    except Exception as e:
        log.error(f"Не удалось запустить DM-агента: {e}")
        return False

def ensure_dm_agent_alive():
    """Проверяет жив ли DM-агент, перезапускает если нет."""
    try:
        if not find_procs_by_script("dm_agent.py"):
            log.warning("DM-агент не работает — перезапускаю")
            ok = start_dm_agent()
            if ok:
                tg_send("♻️ <b>DM-агент перезапущен</b> — процесс упал и был автоматически поднят")
    except Exception:
        pass

def main():
    log.info("╔" + "═" * 52 + "╗")
    log.info("║  ORCHESTRATOR v3.0  +  Telegram Bot            ║")
    log.info("╚" + "═" * 52 + "╝")

    state = load_state()

    # Диагностика при старте
    state = run_auto_diagnostics(state)
    save_state(state)

    # Запуск DM-агента при старте
    dm_ok = start_dm_agent()
    log.info(f"DM-агент: {'✅ запущен' if dm_ok else '❌ ошибка запуска'}")

    # Регистрируем только нужные команды в меню Telegram
    register_bot_commands()

    tg_send(
        f"<b>🤖 Оркестратор v3.0 запущен</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"{'✅' if dm_ok else '❌'} DM-агент\n"
        f"✅ Оркестратор\n\n"
        f"Управление — через кнопки ниже.\n"
        f"Меню (/) — только для команд с текстом.",
        reply_markup=main_keyboard()
    )

    # Запуск Telegram-бота в отдельном потоке
    bot_thread = threading.Thread(
        target=telegram_bot_loop,
        args=(state,),
        daemon=True
    )
    bot_thread.start()

    # Главный планировщик
    tick = 0
    while True:
        try:
            hour = datetime.now().hour
            tick += 1

            if hour == PUBLISH_HOUR and not already_done_today(state, "last_publish_date"):
                state = run_publish_cycle(state)
                save_state(state)

            if hour == ANALYTICS_HOUR and not already_done_today(state, "last_analytics_date"):
                state = run_analytics_cycle(state)
                save_state(state)

            if hour == AUDIT_HOUR and not already_done_today(state, "last_audit_date"):
                state = run_system_audit(state)
                save_state(state)

            if not done_within_hours(state, "last_optimize_time", OPTIMIZE_EVERY_HOURS):
                state = run_self_optimization(state)
                save_state(state)

            # Каждые 5 минут проверяем DM-агента
            if tick % 5 == 0:
                ensure_dm_agent_alive()

            # Каждый час — автодиагностика
            if tick % 60 == 0:
                state = run_auto_diagnostics(state)
                save_state(state)

            # Каждые 3 дня — исследование трендов
            last_research = state.get("last_research_date")
            research_due = True
            if last_research:
                try:
                    diff = (datetime.now() - datetime.fromisoformat(last_research)).days
                    research_due = diff >= RESEARCH_EVERY_DAYS
                except Exception:
                    pass
            if research_due and hour == ANALYTICS_HOUR - 1:
                log.info("🔍 Запуск Research Agent")
                ok, out = run_script("research_agent.py")
                state["last_research_date"] = datetime.now().isoformat()
                save_state(state)
                if not ok:
                    log.warning(f"Research Agent ошибка: {out[-200:]}")

            # Еженедельно — стратегический анализ
            last_strategy = state.get("last_strategy_date")
            strategy_due = True
            if last_strategy:
                try:
                    diff = (datetime.now() - datetime.fromisoformat(last_strategy)).days
                    strategy_due = diff >= STRATEGY_EVERY_DAYS
                except Exception:
                    pass
            if strategy_due and hour == ANALYTICS_HOUR:
                log.info("📊 Запуск Strategy Agent")
                ok, out = run_script("strategy_agent.py")
                state["last_strategy_date"] = datetime.now().isoformat()
                save_state(state)
                if not ok:
                    log.warning(f"Strategy Agent ошибка: {out[-200:]}")

            time.sleep(60)

        except KeyboardInterrupt:
            log.info("⛔ Остановлен вручную")
            tg_send("⛔ <b>Оркестратор остановлен</b>")
            break
        except Exception as e:
            log.error(f"Ошибка: {e}\n{traceback.format_exc()}")
            tg_send(f"❌ <b>Ошибка оркестратора</b>\n{str(e)[:300]}")
            time.sleep(300)

if __name__ == "__main__":
    main()
