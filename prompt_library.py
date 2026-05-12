"""
Prompt Library v1.0
===================
Централизованное хранилище промптов.
Все агенты загружают промпты отсюда — не хардкодят в коде.

Использование:
    from prompt_library import get_prompt
    prompt = get_prompt("carousel")       # читает prompts/carousel.txt
    prompt = get_prompt("research")       # читает prompts/research.txt

Кастомизация:
    Просто редактируй файлы в папке prompts/ — изменения применятся
    при следующем запуске агента без перезапуска системы.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Встроенные промпты — запасной вариант если файл не найден
_DEFAULTS = {
    "research": """Ты эксперт по Instagram-маркетингу. Найди одну ТРЕНДОВУЮ тему для обучающего контента.
Аудитория: эксперты, блогеры, малый бизнес.
Верни строго JSON: {"topic":"...","angle":"...","pain":"...","format":"carousel","trigger_word":"ГАЙД"}""",

    "carousel": """Ты SMM-эксперт. Создай обучающую карусель для Instagram.
Верни строго JSON с полями: slide1_title, slide1_subtitle, tips (num/title/body/example), cta_slide, caption, hashtags""",

    "reels": """Ты создатель вирусных Reels. Напиши скрипт.
Верни строго JSON: hook, tip1, tip2, tip3, cta, caption, hashtags""",

    "optimization": """Ты оптимизатор системы. Анализируй данные и верни JSON:
{"auto_actions": [], "manual_recommendations": []}""",
}


def get_prompt(name: str) -> str:
    """
    Загружает промпт по имени.
    Сначала ищет prompts/{name}.txt, при отсутствии — возвращает дефолтный.

    Args:
        name: имя промпта (research / carousel / reels / dm_reply / optimization)

    Returns:
        Текст промпта
    """
    path = PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"  [PromptLibrary] Ошибка чтения {name}.txt: {e}")

    # Fallback на встроенный дефолт
    if name in _DEFAULTS:
        print(f"  [PromptLibrary] Используется дефолтный промпт для '{name}'")
        return _DEFAULTS[name]

    raise FileNotFoundError(f"Промпт '{name}' не найден в {PROMPTS_DIR}")


def list_prompts() -> list[str]:
    """Возвращает список доступных промптов."""
    files = list(PROMPTS_DIR.glob("*.txt")) if PROMPTS_DIR.exists() else []
    return [f.stem for f in sorted(files)]


def reload_prompt(name: str) -> str:
    """Принудительно перечитывает промпт с диска (для горячей замены)."""
    return get_prompt(name)


def update_prompt(name: str, content: str) -> bool:
    """
    Обновляет промпт из кода (например из Telegram-команды).
    Создаёт папку prompts/ если не существует.
    """
    try:
        PROMPTS_DIR.mkdir(exist_ok=True)
        path = PROMPTS_DIR / f"{name}.txt"
        path.write_text(content.strip(), encoding="utf-8")
        print(f"  [PromptLibrary] Обновлён: {name}.txt")
        return True
    except Exception as e:
        print(f"  [PromptLibrary] Ошибка записи {name}: {e}")
        return False


if __name__ == "__main__":
    print("Prompt Library — доступные промпты:")
    for name in list_prompts():
        txt = get_prompt(name)
        print(f"  {name}: {len(txt)} символов")
