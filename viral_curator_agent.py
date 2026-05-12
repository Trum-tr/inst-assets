"""
Viral Curator Agent v2.0 — Ручной режим
=========================================
Принимает username или ссылку на Reel, анализирует GPT-4o,
накладывает аватар + разбор, репостит от @inst.insider.ru.

Запуск:
  python viral_curator_agent.py @username        — взять самый вирусный пост аккаунта
  python viral_curator_agent.py @username --dry  — анализ без публикации
  python viral_curator_agent.py --dry            — тест (используется аккаунт из DEMO_ACCOUNT)
"""

import os, sys, json, time, re, subprocess, tempfile
from pathlib import Path
from datetime import datetime
from io import BytesIO

# ── Зависимости ───────────────────────────────────────────────────────────────
for pkg in ["instagrapi", "openai", "requests", "Pillow", "moviepy",
            "imageio-ffmpeg", "python-dotenv"]:
    try:
        __import__(pkg.replace("-", "_").split("==")[0])
    except ImportError:
        print(f"  Устанавливаю {pkg}...")
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"])

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import requests as http_requests
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from instagrapi import Client

# ── Конфиг ────────────────────────────────────────────────────────────────────
BASE           = Path(__file__).parent
SESSION_FILE   = BASE / "ig_session.json"
PROCESSED_FILE = BASE / "viral_processed.json"

IG_USERNAME  = os.getenv("IG_USERNAME", "inst.insider.ru")
IG_PASSWORD  = os.getenv("IG_PASSWORD")
OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

DRY_RUN      = "--dry" in sys.argv

# Аккаунт для демо-теста (--dry без username)
DEMO_ACCOUNT = "pozovi_menya_sami_placeholder"  # заменяется на реальный в viral_accounts.json

MIN_VIEWS    = 30_000
MIN_LIKES    = 3_000

client_ai = OpenAI(api_key=OPENAI_KEY)

# ── Утилиты ───────────────────────────────────────────────────────────────────
def load_processed() -> set:
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()

def save_processed(ids: set):
    PROCESSED_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )

def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"  Telegram: {e}")

def _format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}М"
    if n >= 1_000:
        return f"{n//1_000}к"
    return str(n)

# ── Instagram Client ──────────────────────────────────────────────────────────
def build_client() -> Client:
    cl = Client()
    cl.delay_range = [3, 7]
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()
            print("  Сессия Instagram восстановлена")
            return cl
        except Exception as e:
            print(f"  Сессия устарела ({str(e)[:60]}), перелогин...")
            SESSION_FILE.unlink(missing_ok=True)
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    print("  Новая Instagram-сессия сохранена")
    return cl

# ── FINDER: получаем посты одного аккаунта ───────────────────────────────────
def find_best_from_account(cl: Client, username: str, processed: set) -> dict | None:
    """
    Берёт последние посты аккаунта @username,
    возвращает самое вирусное видео которое ещё не обрабатывали.
    """
    print(f"  Загружаю посты @{username}...")
    try:
        user_id = cl.user_id_from_username(username)
        time.sleep(5)
        medias  = cl.user_medias(user_id, amount=15)
        time.sleep(3)
    except Exception as e:
        raise RuntimeError(f"Не могу получить посты @{username}: {e}")

    candidates = []
    for m in medias:
        media_id = str(m.id)
        if media_id in processed:
            continue
        if m.media_type not in (2, 8):
            continue

        views = getattr(m, "view_count", 0) or 0
        likes = getattr(m, "like_count",  0) or 0

        if views >= MIN_VIEWS or likes >= MIN_LIKES:
            candidates.append({
                "id":       media_id,
                "pk":       m.pk,
                "views":    views,
                "likes":    likes,
                "comments": getattr(m, "comment_count", 0) or 0,
                "caption":  (m.caption_text or "")[:500],
                "user":     username,
                "taken_at": m.taken_at.isoformat() if m.taken_at else "",
            })

    if not candidates:
        return None

    candidates.sort(key=lambda x: -(x["views"] or x["likes"] * 10))
    best = candidates[0]
    print(f"  Лучший: {_format_views(best['views'])} просм. / {_format_views(best['likes'])} лайков")
    return best

# ── ANALYZER: GPT-4o разбор ───────────────────────────────────────────────────
def analyze_viral(media_info: dict) -> dict:
    prompt = f"""Ты эксперт по вирусному контенту Instagram.

Данные о Reel:
- Автор: @{media_info['user']}
- Просмотры: {_format_views(media_info['views'])}
- Лайки: {_format_views(media_info['likes'])}
- Комментарии: {media_info['comments']}
- Подпись: {media_info['caption'][:300]}

Аккаунт @inst.insider.ru учит экспертов и блогеров продвигаться в Instagram.

Проанализируй это вирусное видео и верни JSON (без markdown):
{{
  "why_viral": "ОДНА главная причина успеха (1 предложение)",
  "insight_1": "первый вывод для аудитории @inst.insider.ru (конкретный)",
  "insight_2": "второй вывод — другой аспект",
  "insight_3": "третий вывод — что можно применить прямо сейчас",
  "repost_caption": "текст подписи 80-120 слов: анонс разбора + 3 инсайта + CTA написать REELS в директ",
  "caption_short": "одна строка превью до 12 слов"
}}"""

    try:
        resp = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=700,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        result["_views"] = media_info["views"]
        result["_likes"] = media_info["likes"]
        return result
    except Exception as e:
        print(f"  GPT-4o ошибка: {e}")
        return {
            "_views":   media_info["views"],
            "_likes":   media_info["likes"],
            "why_viral": f"Видео набрало {_format_views(media_info['views'])} просмотров благодаря сильному хуку",
            "insight_1": "Хук в первые 3 секунды решает всё",
            "insight_2": "Конкретика и цифры повышают доверие",
            "insight_3": "Чёткий CTA конвертирует просмотры в лиды",
            "repost_caption": (
                f"Разобрал вирусный Reel — {_format_views(media_info['views'])} просмотров 🔥\n\n"
                f"3 инсайта для твоего аккаунта:\n"
                f"1. Сильный хук останавливает скролл\n"
                f"2. Конкретика > красивые слова\n"
                f"3. CTA в конце = прямые обращения\n\n"
                f"Напиши REELS в директ — пришлю гайд прямо сейчас 🎯"
            ),
            "caption_short": f"Разбор вирусного Reel: {_format_views(media_info['views'])} просмотров",
        }

# ── PROCESSOR: оверлей на видео ──────────────────────────────────────────────
def get_avatar_image(cl: Client) -> Image.Image:
    try:
        user_info = cl.user_info_by_username(IG_USERNAME)
        time.sleep(2)
        pic_url   = str(user_info.profile_pic_url)
        resp      = http_requests.get(pic_url, timeout=15)
        avatar    = Image.open(BytesIO(resp.content)).convert("RGBA")

        size = (120, 120)
        avatar = avatar.resize(size, Image.LANCZOS)

        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size[0]-1, size[1]-1), fill=255)

        circle = Image.new("RGBA", size, (0, 0, 0, 0))
        circle.paste(avatar, (0, 0), mask)

        border = Image.new("RGBA", (size[0]+6, size[1]+6), (0, 0, 0, 0))
        ImageDraw.Draw(border).ellipse((0, 0, size[0]+5, size[1]+5), fill=(255,255,255,220))
        border.paste(circle, (3, 3), circle)
        return border
    except Exception as e:
        print(f"  Аватар: {e} — использую заглушку")
        size = (126, 126)
        img  = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((0, 0, size[0]-1, size[1]-1), fill=(255,255,255,220))
        draw.ellipse((3, 3, size[0]-4, size[1]-4), fill=(230, 100, 30, 255))
        draw.text((size[0]//2, size[1]//2), "I", fill=(255,255,255,255),
                  anchor="mm")
        return img


def _wrap_text(text: str, max_chars: int = 40) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= max_chars:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for fp in [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    return ImageFont.load_default()


def add_overlay_to_frame(frame: Image.Image, avatar: Image.Image,
                          analysis: dict, original_user: str) -> Image.Image:
    img  = frame.copy().convert("RGBA")
    W, H = img.size

    # Градиентный баннер снизу
    banner_h  = int(H * 0.36)
    banner_top = H - banner_h
    overlay   = Image.new("RGBA", img.size, (0,0,0,0))
    ov_draw   = ImageDraw.Draw(overlay)

    for y in range(banner_top, H):
        alpha = int(210 * ((y - banner_top) / banner_h) ** 0.5)
        alpha = min(alpha, 210)
        ov_draw.rectangle([(0, y), (W, y)], fill=(0, 0, 0, alpha))

    img  = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    font_title   = _load_font(22)
    font_insight = _load_font(18)
    font_small   = _load_font(14)

    pad_x = 14
    y_cur = banner_top + 8

    views_str = _format_views(analysis.get("_views", 0))
    title = f"🔥 Почему {views_str} просмотров?"
    draw.text((pad_x, y_cur), title, font=font_title, fill=(255, 220, 50, 245))
    y_cur += 30

    insights = [
        analysis.get("insight_1", ""),
        analysis.get("insight_2", ""),
        analysis.get("insight_3", ""),
    ]
    for i, ins in enumerate(insights, 1):
        if not ins:
            continue
        lines = _wrap_text(ins, max_chars=42)
        first = True
        for line in lines[:2]:
            prefix = f"{i}. " if first else "   "
            draw.text((pad_x, y_cur), prefix + line, font=font_insight,
                      fill=(240, 240, 240, 235))
            y_cur += 22
            first  = False
        y_cur += 3

    draw.text((pad_x, H - 20), f"@inst.insider.ru  •  via @{original_user}",
              font=font_small, fill=(200, 200, 200, 190))

    aw, ah = avatar.size
    img.paste(avatar, (W - aw - 10, 10), avatar)

    return img.convert("RGB")


def process_video(video_path: str, avatar: Image.Image, analysis: dict,
                  original_user: str, output_path: str):
    try:
        from moviepy.editor import VideoFileClip
        import numpy as np

        clip = VideoFileClip(video_path)

        def make_frame(t):
            frame  = clip.get_frame(t)
            result = add_overlay_to_frame(Image.fromarray(frame), avatar,
                                          analysis, original_user)
            return np.array(result)

        processed = clip.fl(lambda gf, t: make_frame(t))
        if clip.audio:
            processed = processed.set_audio(clip.audio)

        processed.write_videofile(
            output_path, codec="libx264", audio_codec="aac", logger=None,
            ffmpeg_params=["-crf", "23", "-preset", "fast"],
        )
        clip.close()
        processed.close()
        print(f"  Видео обработано → {Path(output_path).name}")
    except Exception as e:
        raise RuntimeError(f"moviepy: {e}")


# ── PUBLISHER ─────────────────────────────────────────────────────────────────
def publish_reel(cl: Client, video_path: str, caption: str, original_user: str) -> str:
    full_caption = (
        caption +
        f"\n\n📹 via @{original_user}"
        f"\n\n#инстаграм #смм #reels #вирусноевидео #продвижение"
        f" #блогер #маркетинг #instagramtips #contentcreator"
    )
    media = cl.clip_upload(Path(video_path), full_caption)
    return f"https://www.instagram.com/reel/{media.code}/"


# ── ГЛАВНЫЙ ЦИКЛ ──────────────────────────────────────────────────────────────
def run(target_username: str = None):
    print(f"\n{'='*50}")
    print(f"Viral Curator Agent v2.0")
    print(f"{'='*50}")
    print(f"Режим: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"Аккаунт: @{IG_USERNAME}")
    print()

    if not IG_PASSWORD:
        print("IG_PASSWORD не задан в .env"); return
    if not OPENAI_KEY:
        print("OPENAI_API_KEY не задан в .env"); return

    # Определяем целевой аккаунт
    if not target_username:
        # Читаем из viral_accounts.json — берём первый в списке
        accounts_file = BASE / "viral_accounts.json"
        if accounts_file.exists():
            try:
                accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
                target_username = accounts[0].lstrip("@") if accounts else None
            except Exception:
                pass

    if not target_username:
        print("Укажи аккаунт: python viral_curator_agent.py @username")
        print("Или добавь аккаунты в viral_accounts.json")
        send_telegram(
            "⚠️ Viral Curator: не указан аккаунт для мониторинга.\n"
            "Добавь username в viral_accounts.json или используй кнопку 🔥 Вирусный @username"
        )
        return

    target_username = target_username.lstrip("@")
    print(f"Цель: @{target_username}")

    cl = build_client()
    processed = load_processed()
    print(f"  Уже обработано: {len(processed)} видео")

    # 1. Ищем
    print(f"\n[1/4] Поиск вирусного Reel у @{target_username}...")
    try:
        target = find_best_from_account(cl, target_username, processed)
    except RuntimeError as e:
        print(f"  Ошибка: {e}")
        send_telegram(f"❌ Viral Curator: {e}")
        return

    if not target:
        msg = f"@{target_username}: нет новых вирусных видео (порог {_format_views(MIN_VIEWS)} просм.)"
        print(f"  {msg}")
        send_telegram(f"⚠️ Viral Curator: {msg}")
        return

    print(f"  Найдено: {_format_views(target['views'])} просм. | {_format_views(target['likes'])} лайков")

    # 2. Анализируем
    print("\n[2/4] GPT-4o анализ...")
    analysis = analyze_viral(target)
    print(f"  Почему: {analysis.get('why_viral','?')[:80]}")

    if DRY_RUN:
        print("\n[DRY RUN] Без скачивания и публикации.")
        print(f"\nИнсайты:")
        print(f"  1. {analysis.get('insight_1','')}")
        print(f"  2. {analysis.get('insight_2','')}")
        print(f"  3. {analysis.get('insight_3','')}")
        print(f"\nКаптион:\n{analysis.get('repost_caption','')}")
        send_telegram(
            f"<b>🔬 Viral Curator [DRY RUN]</b>\n\n"
            f"Источник: @{target['user']} | {_format_views(target['views'])} просм.\n\n"
            f"<b>Почему вирусное:</b>\n{analysis.get('why_viral','')}\n\n"
            f"<b>Инсайты:</b>\n"
            f"1. {analysis.get('insight_1','')}\n"
            f"2. {analysis.get('insight_2','')}\n"
            f"3. {analysis.get('insight_3','')}\n\n"
            f"<b>Каптион:</b>\n{analysis.get('repost_caption','')[:300]}"
        )
        return

    # 3. Скачиваем + обрабатываем
    print("\n[3/4] Скачивание + наложение оверлея...")
    avatar = get_avatar_image(cl)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            dl_path = cl.video_download(target["pk"], folder=tmp)
            print(f"  Скачано: {Path(str(dl_path)).name}")
        except Exception as e:
            print(f"  Ошибка скачивания: {e}")
            send_telegram(f"❌ Viral Curator: ошибка скачивания: {e}"); return

        output_path = str(tmp / "processed.mp4")
        try:
            process_video(str(dl_path), avatar, analysis, target["user"], output_path)
        except Exception as e:
            print(f"  Ошибка обработки: {e}")
            send_telegram(f"❌ Viral Curator: ошибка обработки: {e}"); return

        # 4. Публикуем
        print("\n[4/4] Публикация...")
        try:
            post_url = publish_reel(cl, output_path, analysis["repost_caption"], target["user"])
            processed.add(target["id"])
            save_processed(processed)

            print(f"  Опубликовано: {post_url}")
            send_telegram(
                f"<b>🔥 Viral Curator — опубликовано!</b>\n\n"
                f"Источник: @{target['user']} | {_format_views(target['views'])} просм.\n\n"
                f"<b>Почему вирусное:</b>\n{analysis.get('why_viral','')}\n\n"
                f"<b>Инсайты в видео:</b>\n"
                f"1. {analysis.get('insight_1','')}\n"
                f"2. {analysis.get('insight_2','')}\n"
                f"3. {analysis.get('insight_3','')}\n\n"
                f"<a href='{post_url}'>Открыть пост</a>"
            )
        except Exception as e:
            print(f"  Ошибка публикации: {e}")
            send_telegram(f"❌ Viral Curator: ошибка публикации: {e}")

    print("\nГотово.")


if __name__ == "__main__":
    # Ищем username в аргументах
    username = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            username = arg.lstrip("@")
            break
    run(username)
