"""
Instagram Growth Content Agent v2.0
=====================================
Ниша: обучающий контент по раскрутке Instagram для блогеров и экспертов.

Что генерирует:
  - Карусели: «5 советов по...» — структурированные, с практическими шагами
  - Reels-скрипты: хук + 3 совета + CTA в директ
  - Подписи, хэштеги, триггер-слова

Запуск:
    python3 content_agent.py

Расписание: cron 9:00 AM ежедневно
"""

import json
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from prompt_library import get_prompt
from passport import get_passport_context

load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME     = os.getenv("SHEET_NAME", "Content Backlog")
CONTENT_COUNT  = int(os.getenv("CONTENT_COUNT", "3"))
CREDS_PATH     = Path(__file__).parent / os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────────────────────────

def parse_json(text):
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except Exception:
                continue
    return json.loads(text)


def load_strategy() -> dict:
    """Читает стратегию от Strategy Agent."""
    path = Path(__file__).parent / "strategy.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def strategy_context(strategy: dict) -> str:
    """Формирует контекст стратегии для промптов."""
    if not strategy:
        return ""
    hot  = "\n".join(f"  - {t}" for t in strategy.get("hot_topics", [])[:4])
    avoid= "\n".join(f"  - {t}" for t in strategy.get("avoid_topics", [])[:3])
    trigs= ", ".join(strategy.get("winning_triggers", []))
    fmt  = strategy.get("preferred_formats", ["carousel"])[0]
    tone = strategy.get("tone", "casual")
    hook = strategy.get("hooks_style", "")
    insight = strategy.get("key_insight", "")
    focus   = strategy.get("next_week_focus", "")

    ctx = f"""СТРАТЕГИЯ (на основе анализа реальных метрик):
Приоритетный формат: {fmt}
Тон: {tone}
Стиль хуков: {hook}
Конвертирующие триггеры: {trigs}

Горячие темы (высокий ER):
{hot or '  — любая тема из списка'}

Темы которые не зашли (избегать):
{avoid or '  — нет ограничений'}

Фокус недели: {focus}
Ключевой инсайт: {insight}"""
    return ctx

def pop_from_backlog() -> dict:
    """Берёт следующую тему из Content Backlog (по приоритету) и помечает как used."""
    path = Path(__file__).parent / "content_backlog.json"
    if not path.exists():
        return {}
    try:
        backlog = json.loads(path.read_text(encoding="utf-8"))
        pending = [x for x in backlog if x.get("status") == "pending"]
        if not pending:
            return {}
        # Берём с наивысшим приоритетом
        item = sorted(pending, key=lambda x: -x.get("priority", 0))[0]
        # Помечаем как использованную
        for x in backlog:
            if x.get("id") == item.get("id"):
                x["status"]  = "used"
                x["used_at"] = datetime.now().isoformat()
                break
        path.write_text(json.dumps(backlog, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [Backlog] Взята тема: {item['topic'][:60]} (priority={item.get('priority',0)})")
        return item
    except Exception as e:
        print(f"  [Backlog] Ошибка: {e}")
        return {}

def research_topic(client, trend_context=""):
    strategy = load_strategy()
    ctx = strategy_context(strategy)

    # Сначала проверяем Content Backlog
    backlog_item = pop_from_backlog()
    if backlog_item:
        return {
            "topic":        backlog_item.get("topic", ""),
            "angle":        backlog_item.get("angle", ""),
            "pain":         backlog_item.get("pain", ""),
            "format":       backlog_item.get("format", "carousel"),
            "trigger_word": backlog_item.get("trigger_word", "ГАЙД"),
        }

    passport_ctx = get_passport_context(["account", "audience", "brand_voice", "content_framework"])
    base_prompt  = get_prompt("research")
    system_prompt = base_prompt
    if passport_ctx:
        system_prompt = passport_ctx + "\n\n" + base_prompt
    if ctx:
        system_prompt += f"\n\n{ctx}"

    messages = [{"role": "system", "content": system_prompt}]
    if trend_context:
        messages.append({"role": "user", "content": f"Актуальные тренды поиска:\n{trend_context}"})

    # Если стратегия рекомендует формат — передаём в запрос
    pref_fmt = strategy.get("preferred_formats", ["carousel"])
    fmt_hint = f"Предпочтительный формат: {pref_fmt[0]}" if pref_fmt else ""
    if fmt_hint:
        messages.append({"role": "user", "content": fmt_hint})

    r = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=400,
        temperature=0.9
    )
    return parse_json(r.choices[0].message.content)


def generate_carousel(client, topic):
    user_msg = (
        f"Тема: {topic['topic']}\n"
        f"Угол: {topic['angle']}\n"
        f"Боль аудитории: {topic['pain']}\n"
        f"Триггер-слово для CTA: {topic['trigger_word']}"
    )
    r = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": get_prompt("carousel")},
            {"role": "user", "content": user_msg}
        ],
        max_tokens=1200,
        temperature=0.85
    )
    return parse_json(r.choices[0].message.content)


def generate_reels(client, topic):
    user_msg = (
        f"Тема: {topic['topic']}\n"
        f"Угол: {topic['angle']}\n"
        f"Боль аудитории: {topic['pain']}\n"
        f"Триггер-слово для CTA: {topic['trigger_word']}"
    )
    r = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": get_prompt("reels")},
            {"role": "user", "content": user_msg}
        ],
        max_tokens=800,
        temperature=0.85
    )
    return parse_json(r.choices[0].message.content)


def get_sheet():
    import gspread
    from google.oauth2 import service_account
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = service_account.Credentials.from_service_account_file(str(CREDS_PATH), scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def next_id(sheet):
    rows = [r for r in sheet.get_all_values()[2:] if r and r[0].startswith("CB-")]
    if not rows:
        return "CB-001"
    nums = []
    for r in rows:
        try:
            nums.append(int(r[0].split("-")[1]))
        except Exception:
            pass
    return f"CB-{(max(nums) + 1):03d}" if nums else "CB-001"


def format_carousel_for_sheet(content):
    tips_text = "\n".join(
        f"{t['num']}. {t['title']}: {t['body']}"
        for t in content.get("tips", [])
    )
    return (
        f"[ОБЛОЖКА] {content.get('slide1_title', '')}\n"
        f"{content.get('slide1_subtitle', '')}\n\n"
        f"[СОВЕТЫ]\n{tips_text}\n\n"
        f"[CTA-СЛАЙД] {content.get('cta_slide', '')}\n\n"
        f"[ПОДПИСЬ] {content.get('caption', '')}"
    )


def format_reels_for_sheet(content):
    return (
        f"[ХУК] {content.get('hook', '')}\n\n"
        f"[СОВЕТ 1] {content.get('tip1', '')}\n"
        f"[СОВЕТ 2] {content.get('tip2', '')}\n"
        f"[СОВЕТ 3] {content.get('tip3', '')}\n\n"
        f"[CTA] {content.get('cta', '')}\n\n"
        f"[ПОДПИСЬ] {content.get('caption', '')}"
    )


def send_telegram(text):
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import requests, warnings
        warnings.filterwarnings("ignore")
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            verify=False, timeout=10
        )
    except Exception as e:
        print(f"⚠️  Telegram: {e}")


def save_local(results):
    path = Path(__file__).parent / "generated_content.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.extend(results)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 Сохранено локально: {path.name}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    from openai import OpenAI
    print("🚀 Instagram Growth Content Agent v2.0\n")
    print("📌 Ниша: обучающий контент по раскрутке Instagram\n")

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Тренды
    try:
        from trends import build_trend_context
        trend_context = build_trend_context(geo="RU")
        print()
    except Exception as e:
        print(f"⚠️  Тренды недоступны: {e}\n")
        trend_context = ""

    # Google Sheets
    sheet = None
    if CREDS_PATH.exists() and SPREADSHEET_ID:
        try:
            sheet = get_sheet()
            print(f"✅ Google Sheets подключён\n")
        except Exception as e:
            print(f"⚠️  Sheets недоступен: {e}\n   Сохраняю локально.\n")
    else:
        print("ℹ️  credentials.json не найден — сохраняю локально.\n")

    results = []
    telegram_summary = []

    for i in range(1, CONTENT_COUNT + 1):
        print(f"── [{i}/{CONTENT_COUNT}] Генерация ────────────────────")
        try:
            topic = research_topic(client, trend_context)
            fmt   = topic.get("format", "carousel")
            print(f"📌 Тема: {topic['topic']}")
            print(f"📐 Формат: {fmt.upper()}")
            print(f"🎯 Триггер: {topic['trigger_word']}")

            if fmt == "reels":
                content = generate_reels(client, topic)
                hook_for_sheet    = content.get("hook", "")
                caption_for_sheet = format_reels_for_sheet(content)
                print(f"🎬 Хук: {hook_for_sheet[:70]}...")
            else:
                content = generate_carousel(client, topic)
                hook_for_sheet    = content.get("slide1_title", "")
                caption_for_sheet = format_carousel_for_sheet(content)
                print(f"🖼️  Обложка: {hook_for_sheet[:70]}")

            hashtags = content.get("hashtags", "")
            cta      = topic.get("trigger_word", "")

            if sheet:
                cid   = next_id(sheet)
                today = datetime.now().strftime("%Y-%m-%d")
                sheet.append_row([
                    cid, today, topic["topic"], fmt.capitalize(),
                    hook_for_sheet, caption_for_sheet,
                    cta, hashtags,
                    "Draft", "", "", "", ""
                ], value_input_option="USER_ENTERED")
                print(f"✅ Записано: {cid}")
            else:
                cid = f"CB-{i:03d}"

            results.append({
                "id": cid,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "topic": topic,
                "content": content,
                "format": fmt
            })

            telegram_summary.append(
                f"{'🖼️' if fmt == 'carousel' else '🎬'} <b>{topic['topic']}</b>\n"
                f"   Триггер: <code>{cta}</code> | {cid}"
            )
            print()

        except json.JSONDecodeError as e:
            print(f"❌ JSON ошибка: {e}\n")
        except Exception as e:
            print(f"❌ Ошибка: {e}\n")

    if results:
        save_local(results)
        print(f"\n✅ Готово! Сгенерировано: {len(results)} единиц контента")

        # ─── Автоматическая генерация слайдов ────────────────────────────────
        print("\n🎨 Запускаю Carousel Generator...\n")
        try:
            from carousel_generator import setup as cg_setup, generate_carousel as cg_make_slides, OUTPUT_DIR as CG_OUTPUT
            cg_setup()
            slide_files = []
            for item in results:
                fmt = item.get("format", "carousel")
                name = item["topic"]["topic"][:50]
                print(f"── {fmt.upper()}: {name}")
                files = cg_make_slides(item, CG_OUTPUT)
                slide_files.extend(files)
                telegram_summary.append(
                    f"🖼 <b>Слайды готовы:</b> {len(files)} PNG → {CG_OUTPUT.name}/{files[0].parent.name}"
                )
            print(f"\n✅ Слайды созданы: {len(slide_files)} PNG")
            print(f"📁 Папка: {CG_OUTPUT}")
        except Exception as e:
            print(f"⚠️  Carousel Generator недоступен: {e}")
        # ─────────────────────────────────────────────────────────────────────

        tg_text = (
            f"📊 <b>Instagram Growth Agent</b>\n"
            f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
            f"Сгенерировано: {len(results)} поста\n\n"
            + "\n".join(telegram_summary)
        )
        send_telegram(tg_text)
        print("📱 Отчёт отправлен в Telegram")
    else:
        print("\n❌ Ничего не сгенерировано")


if __name__ == "__main__":
    main()
