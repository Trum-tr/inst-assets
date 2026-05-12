import urllib.request, json, base64, os, sys

# Читаем токен из .env или аргумента
TOKEN = ""
REPO  = "Trum-tr/inst-assets"

env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    for line in open(env_path, encoding="utf-8"):
        if line.startswith("GITHUB_TOKEN="):
            TOKEN = line.strip().split("=", 1)[1]
        if line.startswith("GITHUB_REPO="):
            REPO = line.strip().split("=", 1)[1]

if not TOKEN:
    print("GITHUB_TOKEN не найден в .env")
    sys.exit(1)

FILES = [
    ("content_agent.py",         "content_agent.py"),
    ("dm_agent.py",              "dm_agent.py"),
    ("orchestrator.py",          "orchestrator.py"),
    ("prompt_library.py",        "prompt_library.py"),
    ("passport.py",              "passport.py"),
    ("strategic_passport.json",  "strategic_passport.json"),
    ("prompts/carousel.txt",     "prompts/carousel.txt"),
    ("prompts/reels.txt",        "prompts/reels.txt"),
    ("prompts/research.txt",     "prompts/research.txt"),
    ("prompts/dm_reply.txt",     "prompts/dm_reply.txt"),
    ("prompts/optimization.txt", "prompts/optimization.txt"),
]

def dl(remote, local):
    url = "https://api.github.com/repos/{}/contents/{}".format(REPO, remote)
    req = urllib.request.Request(url, headers={"Authorization": "token " + TOKEN})
    d   = json.loads(urllib.request.urlopen(req).read())
    folder = os.path.dirname(local)
    if folder:
        os.makedirs(folder, exist_ok=True)
    open(local, "wb").write(base64.b64decode(d["content"]))
    print("  OK  " + local)

print("=== Обновление InstAgent ===")
for remote, local in FILES:
    try:
        dl(remote, local)
    except Exception as e:
        print("  ERR " + local + ": " + str(e))

print("=== Готово! Перезапусти start.bat ===")
