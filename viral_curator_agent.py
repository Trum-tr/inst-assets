"""
Viral Curator Agent v3.0 — Telegram-управление
================================================
Режимы:
  --analyze @username   найти + GPT-анализ → сохранить в viral_pending.json
  --publish-pending     опубликовать из viral_pending.json
  --dry @username       как --analyze, но без сохранения (просто лог)

Вызывается из orchestrator.py — не запускай напрямую.
Для ручного теста: python viral_curator_agent.py --analyze garyvee
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
PENDING_FILE   = BASE / "viral_pending.json"

IG_USERNAME  = os.getenv("IG_USERNAME", "inst.insider.ru")
IG_PASSWORD  = os.getenv("IG_PASSWORD")
OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
TG_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

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

def load_pending() -> dict | None:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None

def save_pending(data: dict):
    PENDING_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def clear_pending():
    PENDING_FILE.unlink(missing_ok=True)

def _format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}М"
    if n >= 1_000:
        return f"{n//1_000}к"
    return str(n) if n else "?"

# ── Instagram Client ──────────────────────────────────────────────────────────
def build_client() -> Client:
    cl = Client()
    cl.delay_range = [3, 7]
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()
            print("  IG сессия восстановлена")
            return cl
        except Exception as e:
            print(f"  Сессия устарела ({str(e)[:60]}), перелогин...")
            SESSION_FILE.unlink(missing_ok=True)
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    print("  Новая IG сессия сохранена")
    return cl

# ── FINDER ────────────────────────────────────────────────────────────────────
def find_best_from_account(cl: Client, username: str, processed: set) -> dict | None:
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
                "pk":       str(m.pk),
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

# ── ANALYZER ──────────────────────────────────────────────────────────────────
def analyze_viral(media_info: dict) -> dict:
    prompt = f"""Ты эксперт по вирусному контенту Instagram.

Данные о Reel:
- Автор: @{media_info['user']}
- Просмотры: {_format_views(media_info['views'])}
- Лайки: {_format_views(media_info['likes'])}
- Комментарии: {media_info['comments']}
- Подпись: {media_info['caption'][:300]}

Аккаунт @inst.insider.ru учит экспертов и блогеров продвигаться в Instagram.

Верни JSON без markdown:
{{
  "why_viral": "ОДНА главная причина успеха (1 предложение)",
  "insight_1": "первый вывод для аудитории @inst.insider.ru (конкретный)",
  "insight_2": "второй вывод — другой аспект",
  "insight_3": "третий вывод — что можно применить прямо сейчас",
  "repost_caption": "подпись 80-120 слов: анонс разбора + 3 инсайта + CTA написать REELS в директ",
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
        print(f"  GPT-4o: {e}")
        return {
            "_views":    media_info["views"],
            "_likes":    media_info["likes"],
            "why_viral": f"{_format_views(media_info['views'])} просмотров — сильный хук и актуальная тема",
            "insight_1": "Хук в первые 3 секунды решает всё",
            "insight_2": "Конкретика и цифры повышают доверие",
            "insight_3": "Чёткий CTA конвертирует просмотры в лиды",
            "repost_caption": (
                f"Разобрал вирусный Reel — {_format_views(media_info['views'])} просмотров 🔥\n\n"
                "3 инсайта для твоего аккаунта:\n"
                "1. Сильный хук останавливает скролл\n"
                "2. Конкретика > красивые слова\n"
                "3. CTA в конце = прямые обращения\n\n"
                "Напиши REELS в директ — пришлю гайд прямо сейчас 🎯"
            ),
            "caption_short": f"Разбор вирусного Reel: {_format_views(media_info['views'])} просмотров",
        }

# ── PROCESSOR: оверлей ────────────────────────────────────────────────────────
def get_avatar_image(cl: Client) -> Image.Image:
    try:
        user_info = cl.user_info_by_username(IG_USERNAME)
        time.sleep(2)
        resp   = http_requests.get(str(user_info.profile_pic_url), timeout=15)
        avatar = Image.open(BytesIO(resp.content)).convert("RGBA")
        size   = (120, 120)
        avatar = avatar.resize(size, Image.LANCZOS)
        mask   = Image.new("L", size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size[0]-1, size[1]-1), fill=255)
        circle = Image.new("RGBA", size, (0,0,0,0))
        circle.paste(avatar, (0,0), mask)
        border = Image.new("RGBA", (size[0]+6, size[1]+6), (0,0,0,0))
        ImageDraw.Draw(border).ellipse((0,0,size[0]+5,size[1]+5), fill=(255,255,255,220))
        border.paste(circle, (3,3), circle)
        return border
    except Exception as e:
        print(f"  Аватар: {e} — заглушка")
        size = (126,126)
        img  = Image.new("RGBA", size, (0,0,0,0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((0,0,size[0]-1,size[1]-1), fill=(255,255,255,220))
        draw.ellipse((3,3,size[0]-4,size[1]-4), fill=(230,100,30,255))
        draw.text((size[0]//2,size[1]//2), "I", fill=(255,255,255,255), anchor="mm")
        return img

def _wrap(text: str, mx: int = 42) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur)+len(w)+1 <= mx: cur = (cur+" "+w).strip()
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines

def _font(size):
    for fp in ["C:/Windows/Fonts/arial.ttf","C:/Windows/Fonts/Arial.ttf",
               "/System/Library/Fonts/Helvetica.ttc",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try: return ImageFont.truetype(fp, size)
        except: pass
    return ImageFont.load_default()

def add_overlay(frame: Image.Image, avatar: Image.Image,
                analysis: dict, original_user: str) -> Image.Image:
    img  = frame.copy().convert("RGBA")
    W, H = img.size
    bh   = int(H * 0.36)
    bt   = H - bh
    ov   = Image.new("RGBA", img.size, (0,0,0,0))
    ovd  = ImageDraw.Draw(ov)
    for y in range(bt, H):
        a = int(210 * ((y-bt)/bh)**0.5)
        ovd.rectangle([(0,y),(W,y)], fill=(0,0,0,min(a,210)))
    img  = Image.alpha_composite(img, ov)
    draw = ImageDraw.Draw(img)
    ft   = _font(22); fi = _font(18); fs = _font(14)
    px, y = 14, bt+8
    draw.text((px,y), f"🔥 Почему {_format_views(analysis.get('_views',0))} просмотров?",
              font=ft, fill=(255,220,50,245))
    y += 30
    for i, key in enumerate(["insight_1","insight_2","insight_3"], 1):
        ins = analysis.get(key, "")
        if not ins: continue
        first = True
        for line in _wrap(ins)[:2]:
            draw.text((px,y), (f"{i}. " if first else "   ")+line,
                      font=fi, fill=(240,240,240,235))
            y += 22; first = False
        y += 3
    draw.text((px,H-20), f"@inst.insider.ru  •  via @{original_user}",
              font=fs, fill=(200,200,200,190))
    aw, ah = avatar.size
    img.paste(avatar, (W-aw-10, 10), avatar)
    return img.convert("RGB")

def process_video(video_path: str, avatar: Image.Image,
                  analysis: dict, original_user: str, output_path: str):
    from moviepy.editor import VideoFileClip
    import numpy as np
    clip = VideoFileClip(video_path)
    def mf(t):
        return np.array(add_overlay(Image.fromarray(clip.get_frame(t)), avatar, analysis, original_user))
    proc = clip.fl(lambda gf, t: mf(t))
    if clip.audio: proc = proc.set_audio(clip.audio)
    proc.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=None,
                         ffmpeg_params=["-crf","23","-preset","fast"])
    clip.close(); proc.close()
    print(f"  Видео готово → {Path(output_path).name}")

def publish_reel(cl: Client, video_path: str, caption: str, original_user: str) -> str:
    full = (caption + f"\n\n📹 via @{original_user}\n\n"
            "#инстаграм #смм #reels #вирусноевидео #продвижение #блогер #instagramtips")
    media = cl.clip_upload(Path(video_path), full)
    return f"https://www.instagram.com/reel/{media.code}/"

# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМЫ ЗАПУСКА
# ══════════════════════════════════════════════════════════════════════════════

def mode_analyze(username: str):
    """
    Находит лучшее видео у @username, анализирует GPT-4o,
    сохраняет в viral_pending.json.
    Оркестратор после этого читает файл и показывает кнопки в Telegram.
    """
    print(f"[ANALYZE] @{username}")
    if not IG_PASSWORD or not OPENAI_KEY:
        print("Нет IG_PASSWORD или OPENAI_API_KEY"); return False

    cl        = build_client()
    processed = load_processed()

    target = find_best_from_account(cl, username, processed)
    if not target:
        print(f"  Нет вирусных видео у @{username} (порог {_format_views(MIN_VIEWS)})")
        save_pending({"error": f"Нет вирусных видео у @{username} (порог {_format_views(MIN_VIEWS)} просмотров)"})
        return False

    print("[GPT-4o] Анализирую...")
    analysis = analyze_viral(target)

    # Сохраняем всё для дальнейшей публикации
    pending = {
        "media":    target,
        "analysis": analysis,
        "analyzed_at": datetime.now().isoformat(),
    }
    save_pending(pending)
    print(f"  Сохранено в viral_pending.json")
    return True


def mode_publish_pending():
    """
    Читает viral_pending.json, скачивает, накладывает оверлей, публикует.
    """
    print("[PUBLISH] Читаю viral_pending.json...")
    pending = load_pending()
    if not pending:
        print("  viral_pending.json не найден — сначала запусти analyze")
        return False
    if "error" in pending:
        print(f"  Ошибка из анализа: {pending['error']}")
        return False

    target   = pending["media"]
    analysis = pending["analysis"]

    if not IG_PASSWORD:
        print("Нет IG_PASSWORD"); return False

    cl = build_client()
    avatar = get_avatar_image(cl)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            dl_path = cl.video_download(target["pk"], folder=tmp)
            print(f"  Скачано: {Path(str(dl_path)).name}")
        except Exception as e:
            print(f"  Ошибка скачивания: {e}"); return False

        output = str(tmp / "processed.mp4")
        try:
            process_video(str(dl_path), avatar, analysis, target["user"], output)
        except Exception as e:
            print(f"  Ошибка обработки: {e}"); return False

        try:
            url = publish_reel(cl, output, analysis["repost_caption"], target["user"])
            processed = load_processed()
            processed.add(target["id"])
            save_processed(processed)
            clear_pending()
            print(f"  Опубликовано: {url}")
            return url
        except Exception as e:
            print(f"  Ошибка публикации: {e}"); return False


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--publish-pending" in args:
        result = mode_publish_pending()
        sys.exit(0 if result else 1)

    # Ищем username
    username = None
    for a in args:
        if not a.startswith("--"):
            username = a.lstrip("@")
            break

    if not username:
        # Берём первый из viral_accounts.json
        af = BASE / "viral_accounts.json"
        if af.exists():
            try:
                accs = json.loads(af.read_text(encoding="utf-8"))
                if accs: username = accs[0].lstrip("@")
            except Exception: pass

    if not username:
        print("Укажи username: python viral_curator_agent.py @username")
        sys.exit(1)

    ok = mode_analyze(username)
    sys.exit(0 if ok else 1)
