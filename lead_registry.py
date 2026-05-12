"""
Lead Registry v2.0 — CRM для Instagram-воронки
================================================
Управляет базой лидов с этапами воронки и историей касаний.

Этапы воронки:
  new       — написал триггер, получил материал
  dialogue  — ведётся диалог
  warm      — интерес подтверждён, думает
  client    — оплатил / стал клиентом
  lost      — не ответил, отказался

Использование:
  from lead_registry import add_lead, update_stage, get_funnel_stats
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

LEADS_FILE = Path(__file__).parent / "lead_registry.json"

STAGES = ["new", "dialogue", "warm", "client", "lost"]

STAGE_LABELS = {
    "new":      "🆕 Новый",
    "dialogue": "💬 Диалог",
    "warm":     "🔥 Тёплый",
    "client":   "💰 Клиент",
    "lost":     "❌ Потерян",
}

TEMP_LABELS = {
    "cold": "❄️",
    "warm": "🌡",
    "hot":  "🔥",
}


def _load() -> list:
    if not LEADS_FILE.exists():
        return []
    try:
        data = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
        # Поддержка старого формата (список без полей CRM)
        result = []
        for item in data:
            if isinstance(item, dict):
                result.append(_normalize(item))
        return result
    except Exception:
        return []


def _save(leads: list):
    LEADS_FILE.write_text(
        json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _normalize(lead: dict) -> dict:
    """Приводит старый формат к новому."""
    return {
        "username":      lead.get("username", "unknown"),
        "date_first":    lead.get("date_first") or lead.get("date", ""),
        "date_last":     lead.get("date_last") or lead.get("date", ""),
        "trigger":       lead.get("trigger", ""),
        "temperature":   lead.get("temperature", "cold"),
        "stage":         lead.get("status") if lead.get("status") in STAGES
                         else lead.get("stage", "new"),
        "touches":       lead.get("touches", 1),
        "notes":         lead.get("notes", ""),
        "followup_sent": lead.get("followup_sent", False),
        "followup_at":   lead.get("followup_at"),
    }


def add_lead(username: str, trigger: str, temperature: str = "cold") -> dict:
    """Добавляет нового лида или обновляет касание существующего."""
    leads = _load()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Если лид уже есть — обновляем касание
    for lead in leads:
        if lead["username"] == username:
            lead["date_last"]  = now
            lead["touches"]   += 1
            lead["trigger"]    = trigger
            # Повышаем температуру если нужно
            temps = ["cold", "warm", "hot"]
            cur_i = temps.index(lead.get("temperature", "cold"))
            new_i = temps.index(temperature)
            if new_i > cur_i:
                lead["temperature"] = temperature
            _save(leads)
            return lead

    # Новый лид
    lead = {
        "username":      username,
        "date_first":    now,
        "date_last":     now,
        "trigger":       trigger,
        "temperature":   temperature,
        "stage":         "new",
        "touches":       1,
        "notes":         "",
        "followup_sent": False,
        "followup_at":   None,
    }
    leads.append(lead)
    _save(leads)
    return lead


def update_stage(username: str, stage: str, note: str = "") -> bool:
    """Обновляет этап воронки для лида."""
    if stage not in STAGES:
        return False
    leads = _load()
    for lead in leads:
        if lead["username"] == username:
            lead["stage"]     = stage
            lead["date_last"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if note:
                lead["notes"] = (lead.get("notes", "") + f"\n[{datetime.now().strftime('%d.%m')}] {note}").strip()
            _save(leads)
            return True
    return False


def mark_followup_sent(username: str):
    """Помечает что follow-up отправлен."""
    leads = _load()
    for lead in leads:
        if lead["username"] == username:
            lead["followup_sent"] = True
            lead["followup_at"]   = datetime.now().strftime("%Y-%m-%d %H:%M")
            _save(leads)
            return


def get_leads_for_followup(hours: int = 24) -> list:
    """Возвращает лидов которым нужен follow-up (прошло N часов, не отвечали)."""
    leads  = _load()
    result = []
    cutoff = datetime.now() - timedelta(hours=hours)
    for lead in leads:
        if lead.get("followup_sent"):
            continue
        if lead.get("temperature") == "hot":
            continue
        if lead.get("stage") in ("client", "lost"):
            continue
        try:
            dt = datetime.strptime(lead["date_last"], "%Y-%m-%d %H:%M")
            if dt <= cutoff:
                result.append(lead)
        except Exception:
            pass
    return result


def get_funnel_stats() -> dict:
    """Возвращает статистику по воронке."""
    leads = _load()
    stats = {s: 0 for s in STAGES}
    temps = {"cold": 0, "warm": 0, "hot": 0}
    triggers = {}
    weekly = 0
    cutoff = datetime.now() - timedelta(days=7)

    for lead in leads:
        stage = lead.get("stage", "new")
        if stage in stats:
            stats[stage] += 1
        t = lead.get("temperature", "cold")
        if t in temps:
            temps[t] += 1
        tr = lead.get("trigger", "?")
        triggers[tr] = triggers.get(tr, 0) + 1
        try:
            dt = datetime.strptime(lead["date_first"], "%Y-%m-%d %H:%M")
            if dt >= cutoff:
                weekly += 1
        except Exception:
            pass

    return {
        "total":    len(leads),
        "weekly":   weekly,
        "stages":   stats,
        "temps":    temps,
        "triggers": dict(sorted(triggers.items(), key=lambda x: -x[1])),
        "clients":  stats.get("client", 0),
        "conversion": round(stats.get("client", 0) / max(len(leads), 1) * 100, 1),
    }


def get_hot_leads(limit: int = 10) -> list:
    """Возвращает горячих лидов и тёплых в диалоге — требуют внимания."""
    leads = _load()
    hot = [l for l in leads
           if l.get("temperature") == "hot"
           or (l.get("temperature") == "warm" and l.get("stage") == "dialogue")]
    hot.sort(key=lambda x: x.get("date_last", ""), reverse=True)
    return hot[:limit]


def format_funnel_telegram(stats: dict) -> str:
    """Форматирует воронку для Telegram."""
    stages  = stats["stages"]
    total   = stats["total"]
    weekly  = stats["weekly"]
    temps   = stats["temps"]
    conv    = stats["conversion"]

    def bar(n, total, width=10):
        filled = round(n / max(total, 1) * width)
        return "█" * filled + "░" * (width - filled)

    top_triggers = "\n".join(
        f"  {t}: {c}" for t, c in list(stats["triggers"].items())[:5]
    )

    hot_leads = get_hot_leads(5)
    hot_lines = "\n".join(
        f"  🔥 @{l['username']} [{l['trigger']}] — {l['stage']}"
        for l in hot_leads
    ) or "  —"

    return (
        f"<b>🎯 Lead Registry — Воронка</b>\n"
        f"Всего: <b>{total}</b> | За неделю: <b>{weekly}</b> | "
        f"Конверсия: <b>{conv}%</b>\n"
        f"{'─'*28}\n\n"
        f"<b>Этапы воронки:</b>\n"
        f"🆕 Новые:    {stages['new']:>3}  {bar(stages['new'], total)}\n"
        f"💬 Диалог:   {stages['dialogue']:>3}  {bar(stages['dialogue'], total)}\n"
        f"🔥 Тёплые:   {stages['warm']:>3}  {bar(stages['warm'], total)}\n"
        f"💰 Клиенты:  {stages['client']:>3}  {bar(stages['client'], total)}\n"
        f"❌ Потеряны: {stages['lost']:>3}  {bar(stages['lost'], total)}\n\n"
        f"<b>Температура:</b>\n"
        f"❄️ Холодные: {temps['cold']} | 🌡 Тёплые: {temps['warm']} | 🔥 Горячие: {temps['hot']}\n\n"
        f"<b>Топ триггеров:</b>\n{top_triggers}\n\n"
        f"<b>Требуют внимания:</b>\n{hot_lines}"
    )


if __name__ == "__main__":
    stats = get_funnel_stats()
    print(format_funnel_telegram(stats))
