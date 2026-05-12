"""
Dashboard Generator v1.0
=========================
Генерирует dashboard.html с актуальными данными.
Открывается в любом браузере — без сервера.

Запуск:
    python dashboard_generator.py
    Затем открыть: C:\InstAgent\dashboard.html
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

BASE = Path(__file__).parent


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def build_daily_leads(leads: list, days: int = 14) -> dict:
    """Строит статистику лидов по дням."""
    result = {}
    for i in range(days):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        result[d] = 0
    for lead in leads:
        try:
            d = lead.get("date_first", lead.get("date", ""))[:10]
            if d in result:
                result[d] += 1
        except Exception:
            pass
    return dict(sorted(result.items()))


def generate_dashboard() -> str:
    """Генерирует полный HTML дашборда."""

    # ── Загружаем данные ──────────────────────────────────────────────────────
    state   = load_json(BASE / "agent_state.json", {})
    leads   = load_json(BASE / "lead_registry.json", [])
    backlog = load_json(BASE / "content_backlog.json", [])
    strategy= load_json(BASE / "strategy.json", {})
    passport= load_json(BASE / "strategic_passport.json", {})

    # ── KPI ───────────────────────────────────────────────────────────────────
    followers = state.get("followers", 0)
    er        = state.get("engagement_rate", 0.0)
    pub_count = state.get("publish_count", 0)
    acc_val   = state.get("account_value", 0)

    kpi = passport.get("kpi_targets", {})
    target_followers = kpi.get("followers", 10000)
    target_er        = kpi.get("engagement_rate", 3.5)
    target_leads     = kpi.get("weekly_leads", 20)
    target_value     = kpi.get("account_value_usd", 5000)

    # ── Воронка лидов ─────────────────────────────────────────────────────────
    stages   = {"new": 0, "dialogue": 0, "warm": 0, "client": 0, "lost": 0}
    triggers = {}
    weekly   = 0
    cutoff   = datetime.now() - timedelta(days=7)
    temps    = {"cold": 0, "warm": 0, "hot": 0}

    for lead in leads:
        s = lead.get("stage", lead.get("status", "new"))
        if s not in stages:
            s = "new"
        stages[s] += 1
        tr = lead.get("trigger", "?")
        triggers[tr] = triggers.get(tr, 0) + 1
        t = lead.get("temperature", "cold")
        if t in temps:
            temps[t] += 1
        try:
            d = lead.get("date_first", lead.get("date", ""))
            dt = datetime.strptime(d[:16], "%Y-%m-%d %H:%M")
            if dt >= cutoff:
                weekly += 1
        except Exception:
            pass

    total_leads  = len(leads)
    conversion   = round(stages["client"] / max(total_leads, 1) * 100, 1)
    daily_leads  = build_daily_leads(leads)

    # ── Backlog ───────────────────────────────────────────────────────────────
    pending_bl = [x for x in backlog if x.get("status") == "pending"]
    used_bl    = [x for x in backlog if x.get("status") == "used"]

    # ── Топ триггеров (JSON для графика) ──────────────────────────────────────
    top_triggers = sorted(triggers.items(), key=lambda x: -x[1])[:6]
    tr_labels = json.dumps([t[0] for t in top_triggers])
    tr_values = json.dumps([t[1] for t in top_triggers])

    # ── Динамика лидов (JSON для графика) ─────────────────────────────────────
    dl_labels = json.dumps(list(daily_leads.keys()))
    dl_values = json.dumps(list(daily_leads.values()))

    # ── Воронка (JSON для графика) ────────────────────────────────────────────
    funnel_labels = json.dumps(["Новые", "Диалог", "Тёплые", "Клиенты", "Потеряны"])
    funnel_values = json.dumps([stages["new"], stages["dialogue"],
                                stages["warm"], stages["client"], stages["lost"]])

    # ── Стратегия ─────────────────────────────────────────────────────────────
    hot_topics   = strategy.get("hot_topics", [])
    avoid_topics = strategy.get("avoid_topics", [])
    key_insight  = strategy.get("key_insight", "—")
    next_focus   = strategy.get("next_week_focus", "—")

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    def pct(val, target):
        return min(round(val / max(target, 1) * 100), 100)

    pct_f  = pct(followers, target_followers)
    pct_er = pct(er, target_er)
    pct_l  = pct(weekly, target_leads)
    pct_v  = pct(acc_val, target_value)

    hot_topics_html   = "".join(f"<li>{t}</li>" for t in hot_topics[:5]) or "<li>—</li>"
    avoid_topics_html = "".join(f"<li>{t}</li>" for t in avoid_topics[:3]) or "<li>—</li>"
    pending_bl_html   = "".join(
        f"<tr><td>{'🖼️' if x.get('format')=='carousel' else '🎬'} {x['topic'][:45]}</td>"
        f"<td>{x.get('trigger_word','')}</td><td>{x.get('priority',0)}</td></tr>"
        for x in sorted(pending_bl, key=lambda x: -x.get("priority", 0))[:8]
    ) or "<tr><td colspan='3'>Бэклог пуст — запусти /research</td></tr>"

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>@inst.insider.ru — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f1117; color: #e0e0e0; padding: 20px; }}
  h1   {{ color: #fff; font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
  .card {{ background: #1a1d27; border-radius: 12px; padding: 20px; }}
  .card h2 {{ font-size: 14px; color: #888; text-transform: uppercase;
              letter-spacing: .5px; margin-bottom: 16px; }}
  .kpi {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .kpi-item {{ background: #22263a; border-radius: 8px; padding: 14px; }}
  .kpi-item .val {{ font-size: 26px; font-weight: 700; color: #fff; }}
  .kpi-item .lbl {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .kpi-item .tgt {{ font-size: 11px; color: #555; margin-top: 4px; }}
  .progress {{ height: 6px; background: #2d3148; border-radius: 3px; margin-top: 8px; }}
  .progress-bar {{ height: 100%; border-radius: 3px;
                   background: linear-gradient(90deg, #6c63ff, #a78bfa); }}
  .progress-bar.green  {{ background: linear-gradient(90deg, #10b981, #34d399); }}
  .progress-bar.orange {{ background: linear-gradient(90deg, #f59e0b, #fbbf24); }}
  .progress-bar.red    {{ background: linear-gradient(90deg, #ef4444, #f87171); }}
  .funnel {{ display: flex; flex-direction: column; gap: 8px; }}
  .funnel-row {{ display: flex; align-items: center; gap: 10px; font-size: 13px; }}
  .funnel-row .stage {{ width: 90px; color: #ccc; }}
  .funnel-row .bar {{ flex: 1; height: 22px; background: #2d3148; border-radius: 4px; overflow: hidden; }}
  .funnel-row .fill {{ height: 100%; border-radius: 4px; display: flex;
                       align-items: center; padding-left: 8px; font-size: 12px;
                       font-weight: 600; color: #fff; min-width: 30px; }}
  .fill-new      {{ background: #3b82f6; }}
  .fill-dialogue {{ background: #8b5cf6; }}
  .fill-warm     {{ background: #f59e0b; }}
  .fill-client   {{ background: #10b981; }}
  .fill-lost     {{ background: #6b7280; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th    {{ color: #888; font-weight: 500; text-align: left; padding: 6px 8px;
           border-bottom: 1px solid #2d3148; }}
  td    {{ padding: 7px 8px; border-bottom: 1px solid #1e2235; color: #ccc; }}
  tr:hover td {{ background: #22263a; }}
  .tag  {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
           font-size: 11px; font-weight: 600; }}
  .tag-hot  {{ background: #7f1d1d; color: #fca5a5; }}
  .tag-warm {{ background: #78350f; color: #fde68a; }}
  .tag-cold {{ background: #1e3a5f; color: #93c5fd; }}
  ul {{ list-style: none; }}
  ul li {{ padding: 5px 0; border-bottom: 1px solid #1e2235;
           font-size: 13px; color: #ccc; }}
  ul li::before {{ content: "→ "; color: #6c63ff; }}
  .insight {{ background: #1e2a4a; border-left: 3px solid #6c63ff;
              padding: 12px 16px; border-radius: 0 8px 8px 0;
              font-size: 13px; color: #c4c4f0; margin-bottom: 12px; }}
  .stat-row {{ display: flex; justify-content: space-between;
               padding: 8px 0; border-bottom: 1px solid #1e2235;
               font-size: 13px; }}
  .stat-val {{ font-weight: 600; color: #fff; }}
  canvas {{ max-height: 200px; }}
</style>
</head>
<body>

<h1>📊 @inst.insider.ru</h1>
<p class="sub">Обновлено: {now_str} &nbsp;·&nbsp; Постов: {pub_count}</p>

<!-- KPI -->
<div class="card" style="margin-bottom:16px;">
  <h2>KPI — Прогресс к целям</h2>
  <div class="kpi">
    <div class="kpi-item">
      <div class="val">{followers:,}</div>
      <div class="lbl">👥 Подписчики</div>
      <div class="tgt">цель: {target_followers:,}</div>
      <div class="progress"><div class="progress-bar {'green' if pct_f>70 else 'orange' if pct_f>30 else 'red'}"
           style="width:{pct_f}%"></div></div>
    </div>
    <div class="kpi-item">
      <div class="val">{er}%</div>
      <div class="lbl">💬 Engagement Rate</div>
      <div class="tgt">цель: {target_er}%</div>
      <div class="progress"><div class="progress-bar {'green' if pct_er>70 else 'orange' if pct_er>30 else 'red'}"
           style="width:{pct_er}%"></div></div>
    </div>
    <div class="kpi-item">
      <div class="val">{weekly}</div>
      <div class="lbl">🎯 Лидов за неделю</div>
      <div class="tgt">цель: {target_leads}</div>
      <div class="progress"><div class="progress-bar {'green' if pct_l>70 else 'orange' if pct_l>30 else 'red'}"
           style="width:{pct_l}%"></div></div>
    </div>
    <div class="kpi-item">
      <div class="val">${acc_val:,}</div>
      <div class="lbl">💰 Стоимость аккаунта</div>
      <div class="tgt">цель: ${target_value:,}</div>
      <div class="progress"><div class="progress-bar {'green' if pct_v>70 else 'orange' if pct_v>30 else 'red'}"
           style="width:{pct_v}%"></div></div>
    </div>
  </div>
</div>

<div class="grid">

  <!-- Воронка лидов -->
  <div class="card">
    <h2>🎯 Воронка лидов</h2>
    <div class="stat-row" style="margin-bottom:12px;">
      <span>Всего лидов</span><span class="stat-val">{total_leads}</span>
    </div>
    <div class="stat-row" style="margin-bottom:12px;">
      <span>Конверсия в клиентов</span><span class="stat-val">{conversion}%</span>
    </div>
    <div class="funnel">
      <div class="funnel-row">
        <span class="stage">🆕 Новые</span>
        <div class="bar"><div class="fill fill-new" style="width:{pct(stages['new'],total_leads)}%">{stages['new']}</div></div>
      </div>
      <div class="funnel-row">
        <span class="stage">💬 Диалог</span>
        <div class="bar"><div class="fill fill-dialogue" style="width:{pct(stages['dialogue'],total_leads)}%">{stages['dialogue']}</div></div>
      </div>
      <div class="funnel-row">
        <span class="stage">🔥 Тёплые</span>
        <div class="bar"><div class="fill fill-warm" style="width:{pct(stages['warm'],total_leads)}%">{stages['warm']}</div></div>
      </div>
      <div class="funnel-row">
        <span class="stage">💰 Клиенты</span>
        <div class="bar"><div class="fill fill-client" style="width:{pct(stages['client'],total_leads)}%">{stages['client']}</div></div>
      </div>
      <div class="funnel-row">
        <span class="stage">❌ Потеряны</span>
        <div class="bar"><div class="fill fill-lost" style="width:{pct(stages['lost'],total_leads)}%">{stages['lost']}</div></div>
      </div>
    </div>
    <div style="margin-top:14px; font-size:12px; color:#888;">
      ❄️ Холодные: {temps['cold']} &nbsp; 🌡 Тёплые: {temps['warm']} &nbsp; 🔥 Горячие: {temps['hot']}
    </div>
  </div>

  <!-- График лидов по дням -->
  <div class="card">
    <h2>📈 Лиды за 14 дней</h2>
    <canvas id="leadsChart"></canvas>
  </div>

  <!-- Топ триггеров -->
  <div class="card">
    <h2>🔑 Топ триггеров</h2>
    <canvas id="triggersChart"></canvas>
  </div>

  <!-- Стратегия -->
  <div class="card">
    <h2>🧠 Стратегия</h2>
    <div class="insight">{key_insight}</div>
    <div style="font-size:12px; color:#888; margin-bottom:6px;">Фокус недели:</div>
    <div style="font-size:13px; color:#c4c4f0; margin-bottom:14px;">{next_focus}</div>
    <div style="font-size:12px; color:#888; margin-bottom:4px;">Горячие темы:</div>
    <ul>{hot_topics_html}</ul>
    <div style="font-size:12px; color:#888; margin-top:10px; margin-bottom:4px;">Избегать:</div>
    <ul>{avoid_topics_html}</ul>
  </div>

  <!-- Content Backlog -->
  <div class="card" style="grid-column: span 2;">
    <h2>📋 Content Backlog — в очереди: {len(pending_bl)} | использовано: {len(used_bl)}</h2>
    <table>
      <tr><th>Тема</th><th>Триггер</th><th>Приоритет</th></tr>
      {pending_bl_html}
    </table>
  </div>

</div>

<script>
const leadsCtx = document.getElementById('leadsChart').getContext('2d');
new Chart(leadsCtx, {{
  type: 'bar',
  data: {{
    labels: {dl_labels},
    datasets: [{{ label: 'Лиды', data: {dl_values},
      backgroundColor: 'rgba(108,99,255,0.6)', borderColor: '#6c63ff',
      borderWidth: 1, borderRadius: 4 }}]
  }},
  options: {{ plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ ticks: {{ color:'#666', maxTicksLimit: 7 }}, grid: {{ color:'#1e2235' }} }},
               y: {{ ticks: {{ color:'#666' }}, grid: {{ color:'#1e2235' }} }} }} }}
}});

const trCtx = document.getElementById('triggersChart').getContext('2d');
new Chart(trCtx, {{
  type: 'doughnut',
  data: {{
    labels: {tr_labels},
    datasets: [{{ data: {tr_values},
      backgroundColor: ['#6c63ff','#10b981','#f59e0b','#3b82f6','#ef4444','#8b5cf6'],
      borderWidth: 0 }}]
  }},
  options: {{ plugins: {{ legend: {{ position:'right', labels: {{ color:'#ccc', font: {{ size:12 }} }} }} }} }}
}});
</script>
</body>
</html>"""

    return html


def save_dashboard() -> Path:
    """Генерирует и сохраняет dashboard.html."""
    html = generate_dashboard()
    out  = BASE / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = save_dashboard()
    print(f"Dashboard сохранён: {path}")
