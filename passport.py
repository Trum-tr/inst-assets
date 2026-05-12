"""
Strategic Passport v1.0
=======================
Централизованный документ с позиционированием аккаунта.
Все агенты читают отсюда — не хардкодят в промптах.

Использование:
    from passport import get_passport_context, load_passport, update_passport
    ctx = get_passport_context()   # вставляется в system prompt агентов
"""

import json
from pathlib import Path
from datetime import datetime

PASSPORT_PATH = Path(__file__).parent / "strategic_passport.json"


def load_passport() -> dict:
    """Загружает паспорт с диска."""
    if not PASSPORT_PATH.exists():
        return {}
    try:
        return json.loads(PASSPORT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [Passport] Ошибка чтения: {e}")
        return {}


def save_passport(data: dict) -> bool:
    """Сохраняет паспорт на диск."""
    try:
        data["_meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        PASSPORT_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception as e:
        print(f"  [Passport] Ошибка записи: {e}")
        return False


def get_passport_context(sections: list = None) -> str:
    """
    Формирует строку контекста для вставки в system prompt агентов.

    Args:
        sections: список разделов для включения.
                  None = все разделы.
                  Варианты: ['account', 'audience', 'brand_voice',
                             'content_framework', 'monetization_funnel']
    """
    p = load_passport()
    if not p:
        return ""

    parts = []

    if sections is None or "account" in sections:
        acc = p.get("account", {})
        parts.append(
            f"АККАУНТ: @{acc.get('username','')}\n"
            f"Ниша: {acc.get('niche','')}\n"
            f"Позиционирование: {acc.get('positioning','')}\n"
            f"УТП: {acc.get('usp','')}"
        )

    if sections is None or "audience" in sections:
        aud = p.get("audience", {})
        pri = aud.get("primary", {})
        sec = aud.get("secondary", {})
        parts.append(
            f"АУДИТОРИЯ:\n"
            f"Основная — {pri.get('segment','')}: {pri.get('pain','')}\n"
            f"  Хотят: {pri.get('desire','')}\n"
            f"Вторичная — {sec.get('segment','')}: {sec.get('pain','')}"
        )

    if sections is None or "brand_voice" in sections:
        bv = p.get("brand_voice", {})
        forbidden = "; ".join(bv.get("forbidden", [])[:3])
        parts.append(
            f"ГОЛОС БРЕНДА:\n"
            f"Тон: {bv.get('tone','')}\n"
            f"Стиль: {bv.get('style','')}\n"
            f"Хуки: {bv.get('hooks_style','')}\n"
            f"Запрещено: {forbidden}"
        )

    if sections is None or "content_framework" in sections:
        cf = p.get("content_framework", {})
        pillars = cf.get("content_pillars", [])
        pillar_lines = "\n".join(
            f"  - {pl['pillar']} ({pl['weight']}%): {', '.join(pl['topics'][:2])}"
            for pl in pillars
        )
        forbidden_topics = "; ".join(cf.get("forbidden_topics", []))
        parts.append(
            f"КОНТЕНТНЫЕ РАМКИ:\n"
            f"Форматы: {cf.get('formats', {}).get('ratio','')}\n"
            f"Контентные столпы:\n{pillar_lines}\n"
            f"Не публикуем: {forbidden_topics}"
        )

    if sections is None or "monetization_funnel" in sections:
        mf = p.get("monetization_funnel", {})
        triggers = ", ".join(mf.get("triggers", []))
        parts.append(
            f"МОНЕТИЗАЦИЯ:\n"
            f"Воронка: {mf.get('top','')} → {mf.get('middle','')} → {mf.get('bottom','')}\n"
            f"Триггеры директа: {triggers}"
        )

    if not parts:
        return ""

    return "СТРАТЕГИЧЕСКИЙ ПАСПОРТ @inst.insider.ru:\n" + "\n\n".join(parts)


def update_passport(field_path: str, value) -> bool:
    """
    Обновляет поле паспорта по пути через точку.
    Например: update_passport("brand_voice.tone", "более провокационный")
    """
    p = load_passport()
    if not p:
        return False

    keys = field_path.split(".")
    ref = p
    for key in keys[:-1]:
        if key not in ref:
            ref[key] = {}
        ref = ref[key]
    ref[keys[-1]] = value

    # Логируем изменение
    update_log = p.setdefault("_meta", {}).setdefault("updates_log", [])
    update_log.append({
        "date":  datetime.now().isoformat(),
        "field": field_path,
        "value": str(value)[:100],
    })
    p["_meta"]["updates_log"] = update_log[-50:]

    return save_passport(p)


def apply_strategy_update(strategy: dict) -> bool:
    """
    Обновляет паспорт на основе данных от Strategy Agent.
    Вызывается после каждого запуска strategy_agent.py.
    """
    p = load_passport()
    if not p:
        return False

    updates_made = []

    # Обновляем лучшее время публикации
    best_hour = strategy.get("best_publish_hour")
    if best_hour is not None:
        times = p.setdefault("content_framework", {}).setdefault("best_times", [])
        new_time = f"{best_hour:02d}:00"
        if new_time not in times:
            p["content_framework"]["best_times"] = [new_time] + times[:1]
            updates_made.append(f"best_publish_hour → {new_time}")

    # Обновляем предпочтительный формат
    pref_formats = strategy.get("preferred_formats", [])
    if pref_formats:
        fmt = pref_formats[0]
        p.setdefault("content_framework", {}).setdefault("formats", {})["primary"] = fmt
        updates_made.append(f"primary_format → {fmt}")

    # Добавляем инсайт в историю обновлений
    insight = strategy.get("key_insight", "")
    focus   = strategy.get("next_week_focus", "")
    if insight or focus:
        record = {
            "date":    datetime.now().strftime("%Y-%m-%d"),
            "insight": insight,
            "focus":   focus,
            "updates": updates_made,
        }
        log = p.setdefault("strategy_updates", [])
        log.append(record)
        p["strategy_updates"] = log[-10:]

    if updates_made:
        print(f"  [Passport] Обновлено: {', '.join(updates_made)}")

    return save_passport(p)


def passport_summary() -> str:
    """Краткая сводка паспорта для Telegram."""
    p = load_passport()
    if not p:
        return "Паспорт не найден."

    acc = p.get("account", {})
    bv  = p.get("brand_voice", {})
    cf  = p.get("content_framework", {})
    mf  = p.get("monetization_funnel", {})
    upd = p.get("_meta", {}).get("updated_at", "—")

    pillars = cf.get("content_pillars", [])
    pillar_lines = "\n".join(
        f"  {pl['weight']}% {pl['pillar']}" for pl in pillars
    )

    triggers = ", ".join(mf.get("triggers", []))
    times    = ", ".join(cf.get("best_times", []))
    fmt_ratio = cf.get("formats", {}).get("ratio", "—")

    return (
        f"<b>📋 Strategic Passport</b>\n"
        f"@{acc.get('username','')} | обновлён {upd}\n"
        f"{'─'*28}\n\n"
        f"<b>Позиционирование:</b>\n{acc.get('positioning','—')}\n\n"
        f"<b>УТП:</b>\n{acc.get('usp','—')}\n\n"
        f"<b>Голос:</b> {bv.get('tone','—')}\n\n"
        f"<b>Контентные столпы:</b>\n{pillar_lines}\n\n"
        f"<b>Форматы:</b> {fmt_ratio}\n"
        f"<b>Лучшее время:</b> {times}\n"
        f"<b>Триггеры директа:</b> {triggers}"
    )


if __name__ == "__main__":
    print(get_passport_context())
