#!/usr/bin/env python3
"""
Telegram-бот: пишешь вопрос, получаешь ответ от DeepSeek.
Режим /физика: полные решения (Дано, формулы, подстановка, ответ) для задач с рисунками.
Рисунок ты описываешь текстом, а для типовой таблицы из 15 величин есть точный расчёт в Python.
Только стандартная библиотека Python 3.8+.

Запуск:  python bot.py
"""
import json
import math
import re
import time
import urllib.error
import urllib.request

# ======== НАСТРОЙКИ ========
TELEGRAM_TOKEN = "9068326594:AAHmjMGmUJj7vDylEDunPb0ip0YtCho6G2Y"     # токен от @BotFather
DEEPSEEK_KEY = "sk-3b75aa5df3fd414b99e4fe9c2e2b60b8"              # ключ с platform.deepseek.com
ALLOWED_CHAT_ID = 0                  # твой chat id (0 = отвечать всем, см. ниже как узнать)
MAX_HISTORY = 12                     # сколько последних сообщений помнить (6 вопросов и 6 ответов)
MODEL = "deepseek-chat"              # обычный режим: быстро
PHYS_MODEL = "deepseek-reasoner"     # режим /физика: думает дольше, но точнее считает
SYSTEM = ("Отвечай максимально коротко: только итог без пояснений. НО если просят развернуто пиши с решениями. "
          "Для заданий с несколькими пунктами: по строке на пункт, например «1) 12» «2) 11». "
          "Отвечай на языке вопроса. "
          "Пиши обычным текстом, без LaTeX и markdown.")
PHYS_SYSTEM = (
    "Ты решаешь школьные задачи по физике. Пользователь переписывает условие и описывает рисунок текстом: "
    "точки с координатами, векторы (проекциями или длиной и направлением), масштаб.\n"
    "Оформляй решение как положено:\n"
    "1) Дано (значения переведи в СИ);\n"
    "2) формулы и их краткий вывод;\n"
    "3) подстановка чисел;\n"
    "4) ответ с единицами измерения.\n"
    "Если в задаче таблица, заполни все пункты по порядку: у каждого формула, подстановка и результат. "
    "Слово «таблица» от пользователя означает стандартную таблицу из 15 величин: "
    "1 модуль перемещения S; 2 модуль силы F (и проекции Fx, Fy); 3 проекция силы на направление перемещения Fs; "
    "4 работа силы A; 5 модуль начального импульса p1; 6 модуль конечного импульса p2; "
    "7 модуль импульса силы Ft; 8 начальная кинетическая энергия Ek1; 9 конечная кинетическая энергия Ek2; "
    "10 модуль ускорения a; 11 время движения t; 12 проекция ускорения на OX; 13 проекция ускорения на OY; "
    "14 угол между ускорением и OY (α); 15 угол между силой и перемещением (β). "
    "Подсказка: при постоянной силе перемещение = средняя скорость · время, ускорение = (v2 − v1)/t.\n"
    "Если по описанию рисунка не хватает данных, не придумывай их, а коротко спроси, чего не хватает. "
    "Если задание требует начертить рисунок, опиши словами, что и как начертить.\n"
    "Пиши обычным текстом без LaTeX и markdown: Ek = m·v²/2, √, Δ, α, β, индексы v1, v2, p1. "
    "Отвечай на языке задачи."
)
# ===========================

PHYS_HELP = (
    "Режим «физика» включён. Решения с формулами и подстановкой.\n\n"
    "Опиши задание текстом: условие, точки с координатами (м), векторы (проекции или длина и направление), "
    "масштаб, что найти. Слово «таблица» = стандартные 15 величин.\n"
    "Пример: m=2 кг. т.1 (20;54), т.2 (40;14). v1 вниз 5 м/с, v2 вправо 6 м/с. таблица\n"
    "Если данных не хватит, спрошу.\n\n"
    "Быстрый вариант для таблицы из 15 величин: только 9 чисел через пробел, точный расчёт:\n"
    "m x1 y1 v1x v1y x2 y2 v2x v2y\n"
    "Пример: 2 20 54 0 -5 40 14 6 0\n\n"
    "Скорость по масштабу: длина стрелки / длина отрезка «4 м/с» * 4. Вниз и влево со знаком минус.\n"
    "/обычный: вернуться в обычный режим."
)


# ---------- Физика (точный расчёт типовой таблицы) ----------

def fmt(x, nd=2):
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s.replace(".", ",")


def p(x):
    """Число для подстановки в формулу: отрицательные берём в скобки."""
    return f"({fmt(x)})" if x < 0 else fmt(x)


def clamp(x):
    return max(-1.0, min(1.0, x))


def is_numbers_only(text):
    return re.fullmatch(r"[\d\s.,;+\-−]+", text) is not None


def parse_numbers(text):
    text = text.replace("−", "-").replace(";", " ")
    return [float(x.replace(",", ".")) for x in re.findall(r"[-+]?\d+(?:[.,]\d+)?", text)]


def solve_physics(nums):
    m, x1, y1, v1x, v1y, x2, y2, v2x, v2y = nums
    dx, dy = x2 - x1, y2 - y1
    S = math.hypot(dx, dy)
    avx, avy = (v1x + v2x) / 2, (v1y + v2y) / 2
    vavg = math.hypot(avx, avy)
    if S == 0 or vavg == 0:
        return "Не могу найти время: перемещение или средняя скорость равны нулю. Проверь числа."
    if m <= 0:
        return "Масса должна быть больше нуля."

    # при постоянной силе: перемещение = средняя скорость * время
    t = S / vavg
    ax, ay = (v2x - v1x) / t, (v2y - v1y) / t
    a = math.hypot(ax, ay)
    Fx, Fy = m * ax, m * ay
    F = m * a
    Fs = (Fx * dx + Fy * dy) / S          # проекция силы на направление перемещения
    A = Fs * S
    v1 = math.hypot(v1x, v1y)
    v2 = math.hypot(v2x, v2y)
    p1, p2 = m * v1, m * v2
    Ft = F * t
    Ek1 = m * v1 ** 2 / 2
    Ek2 = m * v2 ** 2 / 2
    dEk = Ek2 - Ek1
    alpha = math.degrees(math.acos(clamp(ay / a))) if a > 0 else None
    beta = math.degrees(math.acos(clamp(Fs / F))) if F > 0 else None

    lines = [
        f"Дано: m = {fmt(m)} кг; т.1 ({fmt(x1)}; {fmt(y1)}), v₁ = ({fmt(v1x)}; {fmt(v1y)}) м/с; "
        f"т.2 ({fmt(x2)}; {fmt(y2)}), v₂ = ({fmt(v2x)}; {fmt(v2y)}) м/с",
        f"Δx = x₂ − x₁ = {p(x2)} − {p(x1)} = {fmt(dx)} м; Δy = y₂ − y₁ = {p(y2)} − {p(y1)} = {fmt(dy)} м",
        "",
        f"1) S = √(Δx² + Δy²) = √({p(dx)}² + {p(dy)}²) = {fmt(S)} м",
        f"2) F = m·a = {fmt(m)}·{fmt(a)} = {fmt(F)} Н; "
        f"Fx = m·ax = {fmt(m)}·{p(ax)} = {fmt(Fx)} Н; Fy = m·ay = {fmt(m)}·{p(ay)} = {fmt(Fy)} Н",
        f"3) Fs = (Fx·Δx + Fy·Δy)/S = ({p(Fx)}·{p(dx)} + {p(Fy)}·{p(dy)})/{fmt(S)} = {fmt(Fs)} Н",
        f"4) A = Fs·S = {p(Fs)}·{fmt(S)} = {fmt(A)} Дж (проверка: Ek₂ − Ek₁ = {fmt(dEk)} Дж)",
        f"5) p₁ = m·v₁ = {fmt(m)}·√({p(v1x)}² + {p(v1y)}²) = {fmt(m)}·{fmt(v1)} = {fmt(p1)} кг·м/с",
        f"6) p₂ = m·v₂ = {fmt(m)}·√({p(v2x)}² + {p(v2y)}²) = {fmt(m)}·{fmt(v2)} = {fmt(p2)} кг·м/с",
        f"7) Ft = F·t = {fmt(F)}·{fmt(t)} = {fmt(Ft)} Н·с",
        f"8) Ek₁ = m·v₁²/2 = {fmt(m)}·{fmt(v1)}²/2 = {fmt(Ek1)} Дж",
        f"9) Ek₂ = m·v₂²/2 = {fmt(m)}·{fmt(v2)}²/2 = {fmt(Ek2)} Дж",
        f"10) a = √(ax² + ay²) = √({p(ax)}² + {p(ay)}²) = {fmt(a)} м/с²",
        f"11) При постоянной силе Δr = vср·t, vср = (v₁ + v₂)/2 = ({fmt(avx)}; {fmt(avy)}), "
        f"|vср| = {fmt(vavg)} м/с; t = S/|vср| = {fmt(S)}/{fmt(vavg)} = {fmt(t)} с",
        f"12) ax = (v2x − v1x)/t = ({p(v2x)} − {p(v1x)})/{fmt(t)} = {fmt(ax)} м/с²",
        f"13) ay = (v2y − v1y)/t = ({p(v2y)} − {p(v1y)})/{fmt(t)} = {fmt(ay)} м/с²",
        (f"14) cos α = ay/a = {p(ay)}/{fmt(a)}; α = {fmt(alpha, 1)}°" if alpha is not None
         else "14) α не определён (a = 0)"),
        (f"15) cos β = Fs/F = {p(Fs)}/{fmt(F)}; β = {fmt(beta, 1)}°" if beta is not None
         else "15) β не определён (F = 0)"),
        "",
        f"Рис.: перемещение из т.1 в т.2: ({fmt(dx)}; {fmt(dy)}) м. "
        f"Сила из т.2 по направлению ({fmt(Fx)}; {fmt(Fy)}), длина = {fmt(F)} отрезков «1 Н».",
    ]

    # проверка, что числа с рисунка согласованы между собой
    warns = []
    cos_dv = (dx * avx + dy * avy) / (S * vavg)
    if cos_dv < 0.98:
        warns.append(f"перемещение и средняя скорость не сонаправлены (угол {fmt(math.degrees(math.acos(clamp(cos_dv))), 0)}°)")
    if abs(A - dEk) > 0.1 * max(abs(A), abs(dEk), 1e-9):
        warns.append(f"работа {fmt(A)} Дж не равна изменению Ek ({fmt(dEk)} Дж)")
    if warns:
        lines.append("⚠ Проверь числа с рисунка: " + "; ".join(warns) + ".")
    return "\n".join(lines)


# ---------- Telegram и DeepSeek ----------

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


SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def clean(text):
    """Страховка: если модель всё же выдала LaTeX или markdown, превращаем в обычный текст."""
    text = re.sub(r"\\\(|\\\)|\\\[|\\\]|\$\$|\$|\\left|\\right", "", text)
    text = re.sub(r"\\(?:text|mathrm|vec)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    for tex, plain in (("\\cdot", "·"), ("\\times", "×"), ("\\approx", "≈"),
                       ("\\leq", "≤"), ("\\geq", "≥"), ("\\le", "≤"), ("\\ge", "≥"),
                       ("\\Delta", "Δ"), ("\\alpha", "α"), ("\\beta", "β"), ("\\pi", "π"),
                       ("\\rightarrow", "→"), ("\\,", " "), ("\\ ", " ")):
        text = text.replace(tex, plain)
    text = re.sub(r"\^2", "²", text)
    text = re.sub(r"\^3", "³", text)
    text = re.sub(r"_\{?(\d)\}?", lambda m: m.group(1).translate(SUB), text)
    text = re.sub(r"\*\*|__|`", "", text)
    text = text.replace("{", "").replace("}", "")
    return text.strip()


HISTORY = {}  # chat_id -> список последних сообщений (живёт, пока бот запущен)
MODE = {}     # chat_id -> "phys" (если режим физики включён)


def remember(chat_id, question, answer):
    history = HISTORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    del history[:-MAX_HISTORY]


def ask_deepseek(chat_id, question):
    phys = MODE.get(chat_id) == "phys"
    history = HISTORY.setdefault(chat_id, [])
    messages = [{"role": "system", "content": PHYS_SYSTEM if phys else SYSTEM}] \
        + history + [{"role": "user", "content": question}]
    data = post_json(
        "https://api.deepseek.com/chat/completions",
        {"model": PHYS_MODEL if phys else MODEL, "stream": False, "messages": messages},
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        timeout=300,
    )
    answer = clean(data["choices"][0]["message"]["content"])
    remember(chat_id, question, answer)  # запоминаем только после успешного ответа
    return answer


def handle_text(chat_id, text):
    """Возвращает текст ответа."""
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        return "Пиши вопрос, отвечу. /физика: режим задач с рисунком и полными решениями. /clear: забыть разговор."
    if cmd == "/clear":
        HISTORY.pop(chat_id, None)
        return "Забыл."
    if cmd in ("/физика", "/phys"):
        MODE[chat_id] = "phys"
        return PHYS_HELP
    if cmd in ("/обычный", "/normal", "/стоп"):
        MODE.pop(chat_id, None)
        return "Обычный режим."

    if MODE.get(chat_id) == "phys" and is_numbers_only(text):
        nums = parse_numbers(text)
        if len(nums) != 9:
            return f"Нужно 9 чисел через пробел, а пришло {len(nums)}.\nm x1 y1 v1x v1y x2 y2 v2x v2y"
        answer = solve_physics(nums)
        remember(chat_id, "Данные задачи (m x1 y1 v1x v1y x2 y2 v2x v2y): " + text, answer)
        return answer

    return ask_deepseek(chat_id, text)


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

            try:
                tg("sendChatAction", chat_id=chat_id, action="typing")
                send(chat_id, handle_text(chat_id, text))
            except Exception as e:
                print("Ошибка:", e)
                send(chat_id, f"Ошибка: {str(e)[:300]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено")
