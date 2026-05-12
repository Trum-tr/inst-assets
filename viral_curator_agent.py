"""
Viral Curator Agent v1.0
========================
Находит вирусные Reels (50k+ просмотров), анализирует GPT-4o,
накладывает аватар + разбор на видео, репостит от @inst.insider.ru.

Запуск:
  python3 viral_curator_agent.py         — один цикл (найти + опубликовать 1 видео)
  python3 viral_curator_agent.py --dry   — без публикации (только анализ)
"""

import os, sys, json, time, re, math, subprocess, tempfile
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

import requests
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from instagrapi import Client
from instagrapi.exceptions import LoginRequired

# ── Конфиг ────────────────────────────────────────────────────────────────────
BASE          = Path(__file__).parent
SESSION_FILE  = BASE / "ig_session.json"
PROCESSED_FILE= BASE / "viral_processed.json"

IG_USERNAME   = os.getenv("IG_USERNAME", "inst.insider.ru")
IG_PASSWORD   = os.getenv("IG_PASSWORD")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")
TG_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")

DRY_RUN       = "--dry" in sys.argv

# Минимальный порог для считаемого вирусным
MIN_VIEWS     = 50_000
MIN_LIKES     = 5_000

# Топовые аккаунты по нише Instagram-маркетинга для мониторинга
# (метод надёжнее хэштегов — не триггерит challenge_required)
NICHE_ACCOUNTS = [
    "позвоните_мне_сами",     # placeholder — заменить на реальные аккаунты ниши
    "later",
    "hootsuite",
    "sproutsocial",
    "garyvee",
    "mrbeast",
    "hailleyfindlay",
    "socialmediaexaminer",
]

# Хэштеги — используются как запасной метод
NICHE_HASHTAGS = [
    "instagrammarketing",
    "smm",
    "contentcreator",
    "instagramgrowth",
    "reelsinstagram",
]

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
        json.dumps(sorted(ids), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def send_telegram(text: str):
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

# ── Instagram Client ──────────────────────────────────────────────────────────
def build_client() -> Client:
    cl = Client()
    cl.delay_range = [2, 5]
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

# ── FINDER: поиск вирусных Reels ─────────────────────────────────────────────
def _extract_candidate(m, source: str, processed: set) -> dict | None:
    """Извлекает данные из медиа-объекта instagrapi. None если не подходит."""
    media_id = str(m.id)
    if media_id in processed:
        return None
    if m.media_type not in (2, 8):   # 2=video, 8=album
        return None

    views = getattr(m, "view_count", 0) or 0
    likes = getattr(m, "like_count",  0) or 0

    if views < MIN_VIEWS and likes < MIN_LIKES:
        return None

    return {
        "id":       media_id,
        "pk":       m.pk,
        "views":    views,
        "likes":    likes,
        "comments": getattr(m, "comment_count", 0) or 0,
        "caption":  (m.caption_text or "")[:500],
        "user":     m.user.username,
        "source":   source,
        "taken_at": m.taken_at.isoformat() if m.taken_at else "",
    }


def _search_by_accounts(cl: Client, processed: set) -> list:
    """Мониторит посты топ-аккаунтов ниши — надёжный метод без challenge."""
    candidates = []
    # Загружаем список аккаунтов из файла (можно расширять без кода)
    accounts_file = BASE / "viral_accounts.json"
    if accounts_file.exists():
        try:
            accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception:
            accounts = NICHE_ACCOUNTS
    else:
        accounts = NICHE_ACCOUNTS
        # Сохраняем для удобного редактирования
        accounts_file.write_text(
            json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for username in accounts:
        try:
            user_id = cl.user_id_from_username(username)
            medias  = cl.user_medias(user_id, amount=12)
            time.sleep(3)

            for m in medias:
                cand = _extract_candidate(m, f"@{username}", processed)
                if cand:
                    candidates.append(cand)

            print(f"  Аккаунт @{username}: проверено {len(medias)} постов")

        except Exception as e:
            print(f"  @{username}: {str(e)[:60]}")
            time.sleep(3)

    return candidates


def _search_by_hashtags(cl: Client, processed: set) -> list:
    """Поиск по хэштегам — запасной метод (может давать challenge_required)."""
    candidates = []
    for tag in NICHE_HASHTAGS:
        try:
            medias = cl.hashtag_medias_recent(tag, amount=9)
            time.sleep(4)
            for m in medias:
                cand = _extract_candidate(m, f"#{tag}", processed)
                if cand:
                    candidates.append(cand)
            print(f"  Хэштег #{tag}: проверено {len(medias)} постов")
        except Exception as e:
            print(f"  #{tag}: {str(e)[:60]}")
            time.sleep(5)
    return candidates


def find_viral_reels(cl: Client, processed: set, limit: int = 3) -> list:
    """
    Ищет вирусные Reels.
    Сначала мониторит топ-аккаунты (надёжно), при неудаче — хэштеги.
    Возвращает до `limit` самых вирусных видео.
    """
    print("  Метод 1: мониторинг топ-аккаунтов...")
    candidates = _search_by_accounts(cl, processed)

    if not candidates:
        print("  Метод 2: поиск по хэштегам (запасной)...")
        candidates = _search_by_hashtags(cl, processed)

    # Сортируем по просмотрам (лайки × 10 как прокси при отсутствии просмотров)
    candidates.sort(key=lambda x: -(x["views"] or x["likes"] * 10))
    print(f"  Итого кандидатов: {len(candidates)}")
    return candidates[:limit]

# ── ANALYZER: GPT-4o разбор почему видео вирусное ────────────────────────────
def analyze_viral(media_info: dict) -> dict:
    """
    GPT-4o анализирует вирусное видео и возвращает:
    - hook_analysis: что зацепило в первые 3 сек
    - structure: структура видео
    - why_viral: 1 предложение — главная причина успеха
    - insight_1/2/3: 3 конкретных вывода для своей аудитории
    - repost_caption: текст подписи для репоста (на русском)
    - caption_short: 1 строка для Telegram-уведомления
    """
    prompt = f"""Ты эксперт по вирусному контенту Instagram.

Данные о Reel:
- Автор: @{media_info['user']}
- Просмотры: {media_info['views']:,}
- Лайки: {media_info['likes']:,}
- Комментарии: {media_info['comments']:,}
- Подпись: {media_info['caption'][:300]}
- Хэштег, через который нашли: #{media_info['hashtag']}

Аккаунт @inst.insider.ru учит экспертов и блогеров продвигаться в Instagram.

Проанализируй это вирусное видео и верни JSON (без markdown):
{{
  "hook_analysis": "что зацепило зрителя в первые 3 секунды (1-2 предложения)",
  "structure": "краткое описание структуры видео (крючок → тело → CTA)",
  "why_viral": "ОДНА главная причина почему оно набрало {media_info['views']:,} просмотров",
  "insight_1": "первый вывод для аудитории @inst.insider.ru (конкретный, применимый)",
  "insight_2": "второй вывод — другой аспект (крючок / монтаж / тема / аудитория)",
  "insight_3": "третий вывод — что можно украсть из этого видео прямо сейчас",
  "repost_caption": "текст под репостом 80-120 слов: анонс разбора + 3 инсайта + CTA написать REELS в директ",
  "caption_short": "одна строка для превью (до 15 слов)"
}}"""

    try:
        resp = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=800,
        )
        raw = resp.choices[0].message.content.strip()
        # Убираем возможный markdown
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: минимальная структура
        return {
            "hook_analysis": "Сильный крючок привлёк внимание с первых секунд",
            "structure": "Крючок → Польза → CTA",
            "why_viral": f"Видео набрало {media_info['views']:,} просмотров благодаря актуальной теме",
            "insight_1": "Сильный хук останавливает скролл",
            "insight_2": "Конкретика и цифры повышают доверие",
            "insight_3": "Чёткий CTA конвертирует просмотры в лиды",
            "repost_caption": (
                f"Разобрал вирусный Reel с {media_info['views']:,} просмотрами 🔥\n\n"
                f"3 инсайта которые ты можешь применить сегодня:\n"
                f"1. Хук решает всё\n2. Конкретика > абстракция\n3. CTA = деньги\n\n"
                f"Напиши REELS в директ — пришлю гайд по вирусным видео прямо сейчас 🎯"
            ),
            "caption_short": f"Разбор вирусного Reel: {media_info['views']:,} просмотров",
        }
    except Exception as e:
        raise RuntimeError(f"GPT-4o анализ: {e}")

# ── PROCESSOR: наложение оверлея на видео ─────────────────────────────────────
def get_avatar_image(cl: Client) -> Image.Image:
    """Скачивает аватар аккаунта @inst.insider.ru и возвращает круглый PIL Image."""
    try:
        user_info = cl.user_info_by_username(IG_USERNAME)
        pic_url   = str(user_info.profile_pic_url)
        resp      = requests.get(pic_url, timeout=15)
        avatar    = Image.open(BytesIO(resp.content)).convert("RGBA")

        # Ресайз до 120x120
        size   = (120, 120)
        avatar = avatar.resize(size, Image.LANCZOS)

        # Делаем круглую маску
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size[0]-1, size[1]-1), fill=255)

        circle = Image.new("RGBA", size, (0, 0, 0, 0))
        circle.paste(avatar, (0, 0), mask)

        # Белая окантовка 3px
        border = Image.new("RGBA", (size[0]+6, size[1]+6), (0, 0, 0, 0))
        bd_draw = ImageDraw.Draw(border)
        bd_draw.ellipse((0, 0, size[0]+5, size[1]+5), fill=(255, 255, 255, 220))
        border.paste(circle, (3, 3), circle)
        return border

    except Exception as e:
        print(f"  Аватар не загружен ({e}), использую заглушку")
        # Заглушка — оранжевый круг с буквой I
        size   = (126, 126)
        img    = Image.new("RGBA", size, (0, 0, 0, 0))
        draw   = ImageDraw.Draw(img)
        draw   = ImageDraw.Draw(img)
        draw.ellipse((0, 0, size[0]-1, size[1]-1), fill=(255, 255, 255, 220))
        draw.ellipse((3, 3, size[0]-4, size[1]-4), fill=(230, 100, 30, 255))
        try:
            fnt = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 60)
        except Exception:
            fnt = ImageFont.load_default()
        draw.text((size[0]//2, size[1]//2), "I", fill=(255,255,255,255),
                  font=fnt, anchor="mm")
        return img


def _wrap_text(text: str, max_chars: int = 38) -> list[str]:
    """Разбивает текст на строки по max_chars символов."""
    words  = text.split()
    lines  = []
    cur    = ""
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


def add_overlay_to_frame(
    frame: Image.Image,
    avatar: Image.Image,
    analysis: dict,
    original_user: str,
) -> Image.Image:
    """
    Накладывает на кадр:
    - Аватар @inst.insider.ru в правом верхнем углу
    - Полупрозрачный баннер снизу с 3 инсайтами
    - Строка @inst.insider.ru в самом низу
    """
    img  = frame.copy().convert("RGBA")
    W, H = img.size

    draw = ImageDraw.Draw(img)

    # ── Шрифты ────────────────────────────────────────────────────────────────
    font_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
    ]
    def load_font(size):
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
        return ImageFont.load_default()

    font_title   = load_font(22)
    font_insight = load_font(18)
    font_small   = load_font(15)

    # ── Баннер снизу ──────────────────────────────────────────────────────────
    banner_h     = int(H * 0.34)       # ~34% высоты кадра
    banner_top   = H - banner_h
    overlay      = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ov_draw      = ImageDraw.Draw(overlay)

    # Градиентный прямоугольник: снизу непрозрачный, сверху полупрозрачный
    for y in range(banner_top, H):
        alpha = int(200 * ((y - banner_top) / banner_h) ** 0.6)
        alpha = min(alpha, 200)
        ov_draw.rectangle([(0, y), (W, y)], fill=(0, 0, 0, alpha))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # ── Текст: заголовок баннера ───────────────────────────────────────────────
    title  = "🔥 Почему " + _format_views(analysis.get("_views", 0)) + " просмотров?"
    pad_x  = 14
    y_cur  = banner_top + 10

    draw.text((pad_x, y_cur), title, font=font_title, fill=(255, 220, 50, 245))
    y_cur += 30

    # ── Инсайты ───────────────────────────────────────────────────────────────
    insights = [
        analysis.get("insight_1", ""),
        analysis.get("insight_2", ""),
        analysis.get("insight_3", ""),
    ]
    nums = ["1.", "2.", "3."]

    for num, ins in zip(nums, insights):
        if not ins:
            continue
        lines = _wrap_text(ins, max_chars=40)
        first = True
        for line in lines[:2]:  # максимум 2 строки на инсайт
            prefix = num + " " if first else "    "
            draw.text((pad_x, y_cur), prefix + line, font=font_insight,
                      fill=(240, 240, 240, 240))
            y_cur += 22
            first  = False
        y_cur += 2

    # ── Нижняя строка: аккаунт + атрибуция ────────────────────────────────────
    watermark = f"@inst.insider.ru  |  via @{original_user}"
    draw.text((pad_x, H - 22), watermark, font=font_small,
              fill=(200, 200, 200, 200))

    # ── Аватар (правый верхний угол) ──────────────────────────────────────────
    aw, ah = avatar.size
    margin = 10
    ax     = W - aw - margin
    ay     = margin
    img.paste(avatar, (ax, ay), avatar)

    return img.convert("RGB")


def _format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}М"
    if n >= 1_000:
        return f"{n//1_000}к"
    return str(n)


def process_video(
    video_path: str,
    avatar: Image.Image,
    analysis: dict,
    original_user: str,
    output_path: str,
):
    """
    Накладывает оверлей на каждый кадр видео через moviepy.
    Сохраняет результат в output_path.
    """
    try:
        from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip
        import numpy as np

        clip = VideoFileClip(video_path)
        W, H = clip.size

        def make_frame(t):
            frame     = clip.get_frame(t)
            pil_frame = Image.fromarray(frame)
            result    = add_overlay_to_frame(pil_frame, avatar, analysis, original_user)
            return np.array(result)

        processed = clip.fl(lambda gf, t: make_frame(t))
        audio     = clip.audio

        processed = processed.set_audio(audio)
        processed.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None,
            ffmpeg_params=["-crf", "23", "-preset", "fast"],
        )
        clip.close()
        processed.close()
        print(f"  Видео обработано → {output_path}")

    except Exception as e:
        raise RuntimeError(f"moviepy обработка: {e}")


# ── PUBLISHER: загрузка Reel ──────────────────────────────────────────────────
def publish_reel(cl: Client, video_path: str, caption: str, original_user: str) -> str:
    """Загружает видео как Reel, возвращает ссылку на пост."""
    full_caption = (
        caption +
        f"\n\n📹 via @{original_user}"
        f"\n\n#инстаграм #смм #вирусноевидео #reels #contentcreator"
        f" #продвижение #блогер #маркетинг #instagramtips"
    )
    media = cl.clip_upload(Path(video_path), full_caption)
    post_id = media.pk
    return f"https://www.instagram.com/reel/{media.code}/"


# ── ГЛАВНЫЙ ЦИКЛ ──────────────────────────────────────────────────────────────
def run_viral_curator():
    print(f"\n{'='*50}")
    print(f"Viral Curator Agent v1.0")
    print(f"{'='*50}")
    print(f"Режим: {'DRY RUN (без публикации)' if DRY_RUN else 'LIVE'}")
    print(f"Аккаунт: @{IG_USERNAME}")
    print()

    if not IG_PASSWORD:
        print("IG_PASSWORD не задан в .env")
        return
    if not OPENAI_KEY:
        print("OPENAI_API_KEY не задан в .env")
        return

    # Строим клиент
    cl = build_client()

    # Загружаем список уже обработанных видео
    processed = load_processed()
    print(f"  Уже обработано: {len(processed)} видео")

    # 1. Ищем вирусные видео
    print("\n[1/4] Поиск вирусных Reels...")
    candidates = find_viral_reels(cl, processed, limit=5)

    if not candidates:
        msg = "Viral Curator: вирусные Reels не найдены (все уже обработаны или < порога)"
        print(f"  {msg}")
        send_telegram(f"⚠️ {msg}")
        return

    # Берём самое вирусное
    target = candidates[0]
    print(f"  Выбрано: @{target['user']} | {_format_views(target['views'])} просмотров")
    print(f"  Подпись: {target['caption'][:80]}...")

    # 2. Анализируем
    print("\n[2/4] Анализ GPT-4o...")
    analysis = analyze_viral(target)
    analysis["_views"] = target["views"]

    print(f"  Почему вирусное: {analysis.get('why_viral','?')}")
    print(f"  Инсайт 1: {analysis.get('insight_1','?')}")
    print(f"  Инсайт 2: {analysis.get('insight_2','?')}")
    print(f"  Инсайт 3: {analysis.get('insight_3','?')}")

    if DRY_RUN:
        print("\n[DRY RUN] Пропускаю скачивание и публикацию")
        print(f"\nКаптион для репоста:\n{analysis.get('repost_caption','')}")
        send_telegram(
            f"<b>🔬 Viral Curator [DRY RUN]</b>\n\n"
            f"Найдено: @{target['user']} | {_format_views(target['views'])} просм.\n\n"
            f"<b>Почему вирусное:</b>\n{analysis.get('why_viral','')}\n\n"
            f"<b>Инсайт 1:</b> {analysis.get('insight_1','')}\n"
            f"<b>Инсайт 2:</b> {analysis.get('insight_2','')}\n"
            f"<b>Инсайт 3:</b> {analysis.get('insight_3','')}"
        )
        return

    # 3. Скачиваем + обрабатываем
    print("\n[3/4] Скачивание и обработка видео...")

    avatar = get_avatar_image(cl)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Скачиваем оригинал
        try:
            dl_path = cl.video_download(target["pk"], folder=tmp)
            print(f"  Скачано: {dl_path}")
        except Exception as e:
            print(f"  Ошибка скачивания: {e}")
            send_telegram(f"❌ Viral Curator: ошибка скачивания @{target['user']}: {e}")
            return

        output_path = str(tmp / "viral_processed.mp4")

        try:
            process_video(str(dl_path), avatar, analysis, target["user"], output_path)
        except Exception as e:
            print(f"  Ошибка обработки видео: {e}")
            send_telegram(f"❌ Viral Curator: ошибка обработки видео: {e}")
            return

        # 4. Публикуем
        print("\n[4/4] Публикация Reel...")
        try:
            post_url = publish_reel(cl, output_path, analysis["repost_caption"], target["user"])
            print(f"  Опубликовано: {post_url}")

            # Сохраняем в обработанные
            processed.add(target["id"])
            save_processed(processed)

            send_telegram(
                f"<b>🔥 Viral Curator — Репост опубликован!</b>\n\n"
                f"Оригинал: @{target['user']} | {_format_views(target['views'])} просм.\n\n"
                f"<b>Почему вирусное:</b>\n{analysis.get('why_viral','')}\n\n"
                f"<b>Инсайты в разборе:</b>\n"
                f"1. {analysis.get('insight_1','')}\n"
                f"2. {analysis.get('insight_2','')}\n"
                f"3. {analysis.get('insight_3','')}\n\n"
                f"<a href='{post_url}'>Открыть пост</a>"
            )

        except Exception as e:
            print(f"  Ошибка публикации: {e}")
            send_telegram(f"❌ Viral Curator: ошибка публикации: {e}")

    print("\nViral Curator завершён.")


if __name__ == "__main__":
    run_viral_curator()
