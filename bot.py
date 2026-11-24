import json
import time
import urllib.error
import urllib.request

# ======== НАСТРОЙКИ ========
TELEGRAM_TOKEN = "9068326594:AAHmjMGmUJj7vDylEDunPb0ip0YtCho6G2Y"     # токен от @BotFather
DEEPSEEK_KEY = "sk-3b75aa5df3fd414b99e4fe9c2e2b60b8"              # ключ с platform.deepseek.com
ALLOWED_CHAT_ID = 0                  # твой chat id (0 = отвечать всем, см. ниже как узнать)
MODEL = "deepseek-chat"              # "deepseek-reasoner" думает дольше, но точнее в математике
SYSTEM = ("Отвечай максимально коротко: только итог без пояснений. НО если просят развернуто пиши с решениями "
          "Для заданий с несколькими пунктами: по строке на пункт, например «1) 12». 2) 11 "
          "Отвечай на языке вопроса.")
# ===========================


def post_json(url, payload, headers=None, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    return post_json(url, params, timeout=params.get("timeout", 0) + 20)


def send(chat_id, text):
    text = text or "(пустой ответ)"
    for i in range(0, len(text), 4000):  # лимит Telegram 4096 символов
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def ask_deepseek(question):
    data = post_json(
        "https://api.deepseek.com/chat/completions",
        {
            "model": MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": question},
            ],
        },
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        timeout=180,
    )
    return data["choices"][0]["message"]["content"].strip()


def main():
    print("Бот запущен. Остановка: Ctrl+C")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            updates = tg("getUpdates", **params).get("result", [])
        except Exception as e:
            print("Ошибка Telegram:", e)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            if not text or chat_id is None:
                continue

            print(f"chat_id={chat_id}: {text[:80]}")
            if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
                continue  # чужим не отвечаем, чтобы не тратили твой баланс

            if text == "/start":
                send(chat_id, "Пиши вопрос, отвечу.")
                continue

            try:
                tg("sendChatAction", chat_id=chat_id, action="typing")
                send(chat_id, ask_deepseek(text))
            except Exception as e:
                print("Ошибка:", e)
                send(chat_id, f"Ошибка: {str(e)[:300]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено")
