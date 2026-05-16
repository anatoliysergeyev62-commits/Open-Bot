#!/data/data/com.termux/files/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Termux AI Bot v8 — Ultimate Edition
====================================
Исправления v7:
- Исправлены deadlock'ы (RLock), утечки памяти (pending_actions, reminders)
- Исправлена обрезка истории, InputFile, markdown-разбиение
- Усилена sandbox-безопасность Python и shell
- Добавлена поддержка документов (PDF, TXT, PY и др.)
- Полная интеграция Termux API (TTS, уведомления, вибрация, clipboard, battery, wifi, torch)
- Управление пакетами Termux (/pkg)
- Система сессий и алиасов
- Улучшенный веб-поиск с fallback
- Отправка файлов пользователю (/sendfile)
- Graceful shutdown, улучшенное логирование
"""

import os
import sys
import json
import time
import re
import logging
import threading
import subprocess
import requests
import base64
import io
import math
import hashlib
import signal
import atexit
import tempfile
import shutil
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dotenv import load_dotenv
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InputFile,
)

# ========== ENV + LOGGING ==========
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID_STR = os.getenv("ADMIN_USER_ID", "0").strip()
LOG_FILE = os.getenv("LOG_FILE", "/data/data/com.termux/files/home/termux_ai_bot.log")
WORKSPACE = os.getenv("WORKSPACE", "/data/data/com.termux/files/home/ai_workspace")
CUSTOM_CONFIG_FILE = os.getenv("CUSTOM_CONFIG_FILE", "/data/data/com.termux/files/home/ai_custom_config.json")
MEMORY_FILE = os.getenv("MEMORY_FILE", "/data/data/com.termux/files/home/ai_memory.json")
CONTEXT_FILE = os.getenv("CONTEXT_FILE", "/data/data/com.termux/files/home/ai_context_summary.txt")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "/data/data/com.termux/files/home/ai_sessions")
ALIASES_FILE = os.getenv("ALIASES_FILE", "/data/data/com.termux/files/home/ai_aliases.json")

TERMUX_API_AVAILABLE = os.path.exists("/data/data/com.termux/files/usr/bin/termux-api-start")
TERMUX_API_PKG = os.path.exists("/data/data/com.termux/files/usr/bin/termux-notification")

try:
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    print("[FATAL] ADMIN_USER_ID должен быть числом")
    sys.exit(1)

if not BOT_TOKEN:
    print("[FATAL] TELEGRAM_BOT_TOKEN не задан")
    sys.exit(1)
if ADMIN_ID == 0:
    print("[FATAL] ADMIN_USER_ID не задан")
    sys.exit(1)

os.makedirs(WORKSPACE, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "gemini").lower().strip()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-2.5-flash").strip()
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "agent").strip()
CONTEXT_LIMIT = int(os.getenv("CONTEXT_LIMIT", "10"))
KEEP_AFTER_SUMMARY = int(os.getenv("KEEP_AFTER_SUMMARY", "2"))
MAX_HISTORY_PAIRS = 30
PENDING_MAX_AGE = 3600  # 1 час

API_KEYS = {
    "gemini": os.getenv("GEMINI_API_KEY", "").strip(),
    "groq": os.getenv("GROQ_API_KEY", "").strip(),
    "openrouter": os.getenv("OPENROUTER_API_KEY", "").strip(),
}

# ========== CONSTANTS ==========
DANGEROUS_CMD_PATTERNS = [
    re.compile(r'rm\s+(-[rf]*\s+)*[/\s]*\$?\s*/\s*(;|$|\s|&)'),
    re.compile(r'mkfs\.'),
    re.compile(r'dd\s+if\s*=\s*/dev/zero\s+of\s*=/dev/[sh]d'),
    re.compile(r':\(\)\s*\{\s*:\|:\s*&\s*\};\s*:\s*'),
    re.compile(r'>\s*/dev/sd[a-z]'),
    re.compile(r'shutdown\s'),
    re.compile(r'reboot\s'),
    re.compile(r'chmod\s+-R\s+777\s+/'),
    re.compile(r'curl\s+.*\|\s*bash'),
    re.compile(r'wget\s+.*\|\s*bash'),
    re.compile(r'rm\s+-rf\s+~/\.\s*'),
    re.compile(r'rm\s+-rf\s+\.\.'),
    re.compile(r':(){ :|:& };:'),  # fork bomb
    re.compile(r'\bformat\s+/dev/'),
    re.compile(r'\bmkfs\b'),
    re.compile(r'>\s*/dev/null\s*2>&1\s*&&\s*rm\s+-rf\s+/'),
]

INTERACTIVE_CMDS = {"vim", "vi", "nano", "emacs", "less", "more", "top", "htop", "watch", "man", "ssh", "telnet", "ftp", "psql", "mysql", "sqlite3", "python", "python3", "node", "node.js"}

# ========== BASE PROVIDERS ==========
PROVIDERS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "headers": {"Content-Type": "application/json"},
        "parser": "gemini",
        "vision": True,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "headers": {"Authorization": "Bearer {key}", "Content-Type": "application/json"},
        "parser": "openai",
        "vision": False,
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers": {
            "Authorization": "Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://termux-bot.local",
            "X-Title": "Termux AI Bot v8",
        },
        "parser": "openai",
        "vision": True,
    },
}

PROVIDER_MODELS = {
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    ],
    "groq": [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ],
    "openrouter": [
        "anthropic/claude-3.7-sonnet",
        "google/gemini-2.5-flash-exp:free",
        "meta-llama/llama-4-scout",
        "mistralai/mistral-small-3.1-24b-instruct",
        "deepseek/deepseek-chat-v3-0324",
        "openai/gpt-4.1-mini",
    ],
}

# ========== LOAD CUSTOM CONFIG ==========
def load_custom_config():
    if os.path.exists(CUSTOM_CONFIG_FILE):
        try:
            with open(CUSTOM_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for name, cfg in data.get("providers", {}).items():
                    PROVIDERS[name] = cfg
                for name, models in data.get("models", {}).items():
                    if name not in PROVIDER_MODELS:
                        PROVIDER_MODELS[name] = []
                    for m in models:
                        if m not in PROVIDER_MODELS[name]:
                            PROVIDER_MODELS[name].append(m)
                # Load aliases if present
                aliases = data.get("aliases", {})
                if aliases:
                    save_aliases(aliases)
        except Exception as e:
            logger.error(f"Custom config load error: {e}")

def save_custom_config():
    data = {"providers": {}, "models": {}, "aliases": load_aliases()}
    base_provider_names = {"gemini", "groq", "openrouter"}
    for name, cfg in PROVIDERS.items():
        if name not in base_provider_names:
            data["providers"][name] = cfg
    for name, models in PROVIDER_MODELS.items():
        if name not in base_provider_names:
            data["models"][name] = models
    try:
        with open(CUSTOM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Custom config save error: {e}")

# ========== ALIASES ==========
def load_aliases() -> Dict[str, str]:
    if os.path.exists(ALIASES_FILE):
        try:
            with open(ALIASES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_aliases(aliases: Dict[str, str]):
    try:
        with open(ALIASES_FILE, "w", encoding="utf-8") as f:
            json.dump(aliases, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Aliases save error: {e}")

command_aliases = load_aliases()

# ========== MEMORY SYSTEM ==========
def load_memory() -> Dict[str, str]:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_memory(mem: Dict[str, str]):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Memory save error: {e}")

ai_memory = load_memory()

# ========== MODES ==========
MODES = {
    "brief": "Ты — AI-ассистент в Termux на Android. Отвечай максимально кратко, по существу, без воды.",
    "detailed": "Ты — AI-ассистент в Termux на Android. Отвечай развернуто, с объяснениями, но без излишней воды.",
    "technical": "Ты — AI-ассистент в Termux на Android. Отвечай технически точно, с терминами и командами.",
    "code": "Ты — AI-ассистент в Termux на Android. Отвечай только кодом или командами shell. Минимум текста.",
    "agent": (
        "Ты — AI-ассистент в Termux на Android с полным доступом к терминалу и файловой системе. "
        f"Рабочая папка: {WORKSPACE}\n"
        "Доступные инструменты (используй XML-теги):\n"
        "1. <tool>execute_command</tool> + <command>команда</command> — выполнить shell\n"
        "2. <tool>write_file</tool> + <path>путь</path> + <content>содержимое</content> — записать файл\n"
        "3. <tool>read_file</tool> + <path>путь</path> — прочитать файл\n"
        "4. <tool>list_directory</tool> + <path>путь</path> — список файлов\n"
        "5. <tool>search</tool> + <path>путь</path> + <regex>шаблон</regex> — поиск текста в файлах\n"
        "6. <tool>edit_file</tool> + <path>путь</path> + <old_string>что заменить</old_string> + <new_string>на что</new_string> — замена в файле\n"
        "7. <tool>delete_file</tool> + <path>путь</path> — удалить файл\n"
        "8. <tool>memory_save</tool> + <key>ключ</key> + <value>значение</value> — сохранить заметку\n"
        "9. <tool>memory_read</tool> + <key>ключ</key> — прочитать заметку\n"
        "10. <tool>web_search</tool> + <query>запрос</query> — поиск в интернете\n"
        "11. <tool>python</tool> + <code>код</code> — выполнить Python\n"
        "12. <tool>git</tool> + <subcommand>status|diff|log|commit|add</subcommand> + <args>аргументы</args>\n"
        "13. <tool>termux</tool> + <action>notification|toast|vibrate|clipboard|tts|battery|wifi|torch</action> + <args>аргументы</args> — Termux API\n"
        "14. <tool>pkg</tool> + <action>install|remove|search|update|upgrade|list</action> + <args>пакет</args> — управление пакетами\n\n"
        "Правила:\n"
        "- Всегда отвечай кратко. Не объясняй что ты модель. Просто делай.\n"
        "- Для сложных задач используй несколько инструментов последовательно.\n"
        "- При редактировании файлов точно указывай old_string.\n"
        "- Опасные команды (удаление, rm -rf, mkfs) требуют подтверждения пользователя.\n"
        "- Для Termux API используй простые аргументы."
    ),
}

# ========== STATE ==========
class UserState:
    def __init__(self):
        self.provider = DEFAULT_PROVIDER
        self.model = DEFAULT_MODEL
        self.mode = DEFAULT_MODE
        self.messages: List[Dict[str, str]] = []
        self.summary = ""
        self.lock = threading.RLock()
        self.auto_approve = False
        self.shell_mode = False
        self.action_history: List[Dict[str, Any]] = []
        self.reminders: List[threading.Timer] = []
        self.current_session = "default"

user_states: Dict[int, UserState] = {}
states_lock = threading.Lock()

def get_state(user_id: int) -> UserState:
    with states_lock:
        if user_id not in user_states:
            st = UserState()
            st.summary = load_summary()
            user_states[user_id] = st
        return user_states[user_id]

def load_summary() -> str:
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

def save_summary(text: str):
    try:
        with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logger.error(f"Save summary error: {e}")

# ========== SESSIONS ==========
def save_session(state: UserState, name: str):
    path = os.path.join(SESSIONS_DIR, f"{name}.json")
    with state.lock:
        data = {
            "messages": state.messages,
            "summary": state.summary,
            "provider": state.provider,
            "model": state.model,
            "mode": state.mode,
            "saved_at": datetime.now().isoformat(),
        }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True, path
    except Exception as e:
        return False, str(e)

def load_session(state: UserState, name: str):
    path = os.path.join(SESSIONS_DIR, f"{name}.json")
    if not os.path.exists(path):
        return False, "Сессия не найдена"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with state.lock:
            state.messages = data.get("messages", [])
            state.summary = data.get("summary", "")
            state.provider = data.get("provider", DEFAULT_PROVIDER)
            state.model = data.get("model", DEFAULT_MODEL)
            state.mode = data.get("mode", DEFAULT_MODE)
            state.current_session = name
        return True, f"Сессия `{name}` загружена. Пар: {len(state.messages)//2}"
    except Exception as e:
        return False, str(e)

def list_sessions() -> List[str]:
    try:
        files = os.listdir(SESSIONS_DIR)
        return [f.replace(".json", "") for f in files if f.endswith(".json")]
    except Exception:
        return []

# ========== SAFETY ==========
def is_dangerous_command(command: str) -> Tuple[bool, str]:
    cmd_lower = command.lower().strip()
    for pattern in DANGEROUS_CMD_PATTERNS:
        if pattern.search(cmd_lower):
            return True, f"Команда заблокирована опасным паттерном: {pattern.pattern}"
    if re.search(r'rm\s+-[rf]*\s+.*~/\.', cmd_lower):
        return True, "Подозрительное удаление домашней директории"
    # Проверка на интерактивные команды
    first_word = cmd_lower.split()[0] if cmd_lower.split() else ""
    if first_word in INTERACTIVE_CMDS and not cmd_lower.startswith("python -c"):
        return True, f"Интерактивная команда '{first_word}' заблокирована. Используй неинтерактивный режим или /shell с осторожностью."
    return False, ""

def resolve_path(path: str) -> str:
    if not path.startswith("/"):
        path = os.path.join(WORKSPACE, path)
    return os.path.abspath(os.path.expanduser(path))

def is_path_safe(path: str) -> Tuple[bool, str]:
    full = resolve_path(path)
    allowed_prefixes = [
        os.path.abspath(WORKSPACE),
        os.path.abspath(os.path.expanduser("~")),
        "/tmp",
        "/data/data/com.termux/files",
    ]
    for prefix in allowed_prefixes:
        if full.startswith(prefix):
            return True, full
    return False, f"Путь запрещён: {full}. Разрешены только {WORKSPACE}, домашняя директория и /tmp."

# ========== TYPING ==========
def typing_loop(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        time.sleep(4)

# ========== TERMUX API HELPERS ==========
def termux_api_available() -> bool:
    return TERMUX_API_PKG

def termux_notification(title: str, content: str, priority: str = "default") -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        cmd = f'termux-notification --title "{title}" --content "{content}" --priority {priority}'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return "[Уведомление отправлено]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка termux-notification: {e}]"

def termux_toast(message: str) -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        cmd = f'termux-toast "{message}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return "[Toast отправлен]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка termux-toast: {e}]"

def termux_vibrate(duration_ms: int = 300) -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        cmd = f'termux-vibrate -d {duration_ms}'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return "[Вибрация]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка termux-vibrate: {e}]"

def termux_clipboard_get() -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        result = subprocess.run("termux-clipboard-get", shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка clipboard: {e}]"

def termux_clipboard_set(text: str) -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        result = subprocess.run(f'echo "{text}" | termux-clipboard-set', shell=True, capture_output=True, text=True, timeout=10)
        return "[В буфер обмена скопировано]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка clipboard: {e}]"

def termux_tts(text: str) -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        # Сохраняем во временный файл, чтобы избежать проблем с кавычками
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(text)
            tmp = f.name
        result = subprocess.run(f'termux-tts-speak < "{tmp}"', shell=True, capture_output=True, text=True, timeout=30)
        os.unlink(tmp)
        return "[TTS запущен]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка TTS: {e}]"

def termux_battery() -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        result = subprocess.run("termux-battery-status", shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return f"🔋 Батарея: {data.get('percentage', '?')}% | Статус: {data.get('status', '?')} | Темп: {data.get('temperature', '?')}°C"
        return f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка battery: {e}]"

def termux_wifi() -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        result = subprocess.run("termux-wifi-connectioninfo", shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return f"📶 WiFi: {data.get('ssid', '?')} | IP: {data.get('ip', '?')} | Скорость: {data.get('link_speed', '?')} Mbps"
        return f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка wifi: {e}]"

def termux_torch(state: str = "on") -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        cmd = f'termux-torch {state}'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return f"[Фонарик {state}]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка torch: {e}]"

def termux_location() -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        result = subprocess.run("termux-location", shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return f"📍 Lat: {data.get('latitude', '?')} | Lon: {data.get('longitude', '?')} | Точность: {data.get('accuracy', '?')}m"
        return f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка location: {e}]"

def termux_share(text: str = None, file: str = None) -> str:
    if not termux_api_available():
        return "[Termux API не установлен]"
    try:
        if file and os.path.exists(file):
            cmd = f'termux-share -a send "{file}"'
        elif text:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(text)
                tmp = f.name
            cmd = f'termux-share -a send "{tmp}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
            os.unlink(tmp)
            return "[Отправлено через share]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
        else:
            return "[Нет данных для share]"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return "[Отправлено через share]" if result.returncode == 0 else f"[Ошибка: {result.stderr}]"
    except Exception as e:
        return f"[Ошибка share: {e}]"

# ========== TOOL EXECUTION ==========
def tool_execute_command(command: str) -> str:
    dangerous, reason = is_dangerous_command(command)
    if dangerous:
        return f"[БЛОКИРОВКА БЕЗОПАСНОСТИ] {reason}"
    # Проверка алиасов
    parts = command.split()
    if parts and parts[0] in command_aliases:
        command = command.replace(parts[0], command_aliases[parts[0]], 1)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,  # Увеличен таймаут
            cwd=WORKSPACE,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            return f"[EXIT {result.returncode}]\n{err}\n{out}".strip()
        return out or "(команда выполнена, вывод пуст)"
    except subprocess.TimeoutExpired:
        return "[Ошибка: таймаут 120 секунд]"
    except Exception as e:
        return f"[Ошибка выполнения: {e}]"

def tool_read_file(path: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    if not os.path.exists(full):
        return f"[Файл не найден: {full}]"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 8000:
            content = content[:8000] + f"\n\n... [обрезано, всего {len(content)} символов]"
        return content
    except Exception as e:
        return f"[Ошибка чтения: {e}]"

def tool_write_file(path: str, content: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Файл записан: {full}]"
    except Exception as e:
        return f"[Ошибка записи: {e}]"

def tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    if not os.path.exists(full):
        return f"[Файл не найден: {full}]"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if old_string not in content:
            return "[Ошибка: old_string не найдена в файле]"
        content = content.replace(old_string, new_string, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Файл отредактирован: {full}]"
    except Exception as e:
        return f"[Ошибка редактирования: {e}]"

def tool_delete_file(path: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    if not os.path.exists(full):
        return f"[Не найдено: {full}]"
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
            return f"[Папка удалена: {full}]"
        else:
            os.remove(full)
            return f"[Файл удалён: {full}]"
    except Exception as e:
        return f"[Ошибка удаления: {e}]"

def tool_list_directory(path: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    if not os.path.exists(full):
        return f"[Папка не найдена: {full}]"
    try:
        items = os.listdir(full)
        lines = []
        for item in sorted(items):
            item_path = os.path.join(full, item)
            prefix = "📁" if os.path.isdir(item_path) else "📄"
            size = os.path.getsize(item_path) if os.path.isfile(item_path) else "-"
            lines.append(f"{prefix} {item} ({size} bytes)")
        return "\n".join(lines) if lines else "(пусто)"
    except Exception as e:
        return f"[Ошибка: {e}]"

def tool_search(path: str, regex: str) -> str:
    safe, full = is_path_safe(path)
    if not safe:
        return f"[Ошибка доступа: {full}]"
    try:
        pattern = re.compile(regex)
        results = []
        if os.path.isfile(full):
            files = [full]
        else:
            files = []
            for dp, dn, filenames in os.walk(full):
                for f in filenames:
                    files.append(os.path.join(dp, f))
        for file in files[:100]:
            try:
                with open(file, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            results.append(f"{file}:{i}: {line.strip()}")
                            if len(results) >= 50:
                                break
                if len(results) >= 50:
                    break
            except Exception:
                continue
        return "\n".join(results) if results else "(нет результатов)"
    except re.error as e:
        return f"[Ошибка regex: {e}]"
    except Exception as e:
        return f"[Ошибка поиска: {e}]"

def tool_memory_save(key: str, value: str) -> str:
    global ai_memory
    ai_memory[key] = value
    save_memory(ai_memory)
    return f"[Заметка сохранена: {key}]"

def tool_memory_read(key: str) -> str:
    if key in ai_memory:
        return f"[Заметка {key}]: {ai_memory[key]}"
    return f"[Заметка '{key}' не найдена]"

def tool_memory_search(query: str) -> str:
    results = []
    q = query.lower()
    for k, v in ai_memory.items():
        if q in k.lower() or q in v.lower():
            results.append(f"• {k}: {v[:100]}{'...' if len(v) > 100 else ''}")
    return "\n".join(results) if results else "(нет результатов)"

def tool_web_search(query: str) -> str:
    """Улучшенный поиск с fallback на несколько источников."""
    errors = []
    # Попытка 1: DuckDuckGo HTML
    try:
        url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            snippets = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', resp.text)
            snippets = [re.sub(r'<[^>]+>', '', s) for s in snippets]
            if snippets:
                return "Результаты поиска (DuckDuckGo):\n" + "\n".join(f"• {s}" for s in snippets[:8])
    except Exception as e:
        errors.append(f"DDG: {e}")

    # Попытка 2: Wikipedia API
    try:
        url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(query.replace(' ', '_'))}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            extract = data.get("extract", "")
            if extract:
                return f"Результат Wikipedia:\n• {data.get('title', query)}: {extract[:500]}"
    except Exception as e:
        errors.append(f"Wiki: {e}")

    return f"[Поиск не дал результатов. Ошибки: {'; '.join(errors)}]"

def tool_python(code: str) -> str:
    """Усиленная sandbox для Python."""
    output = []
    def safe_print(*args):
        output.append(" ".join(str(a) for a in args))

    allowed_builtins = {
        "len": len, "range": range, "enumerate": enumerate,
        "sum": sum, "min": min, "max": max, "abs": abs,
        "str": str, "int": int, "float": float, "list": list,
        "dict": dict, "tuple": tuple, "set": set, "bool": bool,
        "print": safe_print, "map": map, "filter": filter,
        "zip": zip, "sorted": sorted, "reversed": reversed,
        "round": round, "pow": pow, "divmod": divmod,
        "math": math, "json": json, "re": re,
        "hex": hex, "bin": bin, "oct": oct, "chr": chr, "ord": ord,
        "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
        "type": type, "open": lambda *a, **k: None,  # Блокируем open
    }
    env = {"__builtins__": allowed_builtins}
    try:
        # Предварительная проверка на попытку выхода из sandbox
        forbidden = ['__import__', '__class__', '__bases__', '__subclasses__', 'eval', 'exec', 'compile', 'open', 'os', 'sys', 'subprocess']
        for f in forbidden:
            if f in code:
                return f"[Ошибка: использование '{f}' запрещено в sandbox]"
        exec(code, env, env)
        return "\n".join(output) if output else "(нет вывода)"
    except Exception as e:
        return f"[Ошибка Python: {type(e).__name__}: {e}]"

def tool_git(subcommand: str, args: str = "") -> str:
    allowed = {"status", "diff", "log", "commit", "add", "branch", "checkout", "pull", "push", "clone", "init", "remote", "fetch", "merge", "reset", "revert", "tag", "stash", "show", "blame", "grep", "clean", "mv", "rm"}
    if subcommand not in allowed:
        return f"[Неподдерживаемая git команда: {subcommand}]"
    # Проверка args на инъекцию
    if ";" in args or "|" in args or "&" in args or "$(" in args or "`" in args:
        return "[Ошибка: недопустимые символы в аргументах git]"
    cmd = f"git {subcommand} {args}".strip()
    return tool_execute_command(cmd)

def tool_termux(action: str, args: str = "") -> str:
    actions = {
        "notification": lambda a: termux_notification("Termux Bot", a),
        "toast": lambda a: termux_toast(a),
        "vibrate": lambda a: termux_vibrate(int(a) if a.isdigit() else 300),
        "clipboard_get": lambda a: termux_clipboard_get(),
        "clipboard_set": lambda a: termux_clipboard_set(a),
        "tts": lambda a: termux_tts(a),
        "battery": lambda a: termux_battery(),
        "wifi": lambda a: termux_wifi(),
        "torch": lambda a: termux_torch(a if a in ("on", "off") else "on"),
        "location": lambda a: termux_location(),
        "share": lambda a: termux_share(text=a),
    }
    if action not in actions:
        return f"[Неизвестное Termux API действие: {action}. Доступные: {', '.join(actions.keys())}]"
    return actions[action](args)

def tool_pkg(action: str, args: str = "") -> str:
    allowed = {"install", "remove", "search", "update", "upgrade", "list", "show", "files", "clean"}
    if action not in allowed:
        return f"[Неподдерживаемая pkg команда: {action}]"
    if ";" in args or "|" in args or "&" in args:
        return "[Ошибка: недопустимые символы в аргументах]"
    if action == "update":
        cmd = "pkg update -y"
    elif action == "upgrade":
        cmd = "pkg upgrade -y"
    elif action == "list":
        cmd = "pkg list-installed"
    elif action == "clean":
        cmd = "pkg clean"
    else:
        cmd = f"pkg {action} {args}".strip()
    return tool_execute_command(cmd)

# ========== PARSER TOOLS FROM AI RESPONSE ==========
def parse_tools(text: str) -> List[Dict[str, Any]]:
    actions = []
    tool_blocks = re.findall(
        r'<tool>(\w+)</tool>'
        r'(?:\s*<command>([\s\S]*?)</command>)?'
        r'(?:\s*<path>([\s\S]*?)</path>)?'
        r'(?:\s*<content>([\s\S]*?)</content>)?'
        r'(?:\s*<old_string>([\s\S]*?)</old_string>)?'
        r'(?:\s*<new_string>([\s\S]*?)</new_string>)?'
        r'(?:\s*<regex>([\s\S]*?)</regex>)?'
        r'(?:\s*<key>([\s\S]*?)</key>)?'
        r'(?:\s*<value>([\s\S]*?)</value>)?'
        r'(?:\s*<query>([\s\S]*?)</query>)?'
        r'(?:\s*<code>([\s\S]*?)</code>)?'
        r'(?:\s*<subcommand>([\s\S]*?)</subcommand>)?'
        r'(?:\s*<args>([\s\S]*?)</args>)?'
        r'(?:\s*<action>([\s\S]*?)</action>)?',
        text,
    )
    for block in tool_blocks:
        (tool, command, path, content, old_s, new_s, regex,
         key, value, query, code, subcmd, args, action) = block
        actions.append({
            "tool": (tool or "").strip(),
            "command": (command or "").strip(),
            "path": (path or "").strip(),
            "content": (content or "").strip(),
            "old_string": (old_s or "").strip(),
            "new_string": (new_s or "").strip(),
            "regex": (regex or "").strip(),
            "key": (key or "").strip(),
            "value": (value or "").strip(),
            "query": (query or "").strip(),
            "code": (code or "").strip(),
            "subcommand": (subcmd or "").strip(),
            "args": (args or "").strip(),
            "action": (action or "").strip(),
        })
    return actions

def strip_tools(text: str) -> str:
    cleaned = re.sub(
        r'<tool>\w+</tool>'
        r'(?:\s*<command>[\s\S]*?</command>)?'
        r'(?:\s*<path>[\s\S]*?</path>)?'
        r'(?:\s*<content>[\s\S]*?</content>)?'
        r'(?:\s*<old_string>[\s\S]*?</old_string>)?'
        r'(?:\s*<new_string>[\s\S]*?</new_string>)?'
        r'(?:\s*<regex>[\s\S]*?</regex>)?'
        r'(?:\s*<key>[\s\S]*?</key>)?'
        r'(?:\s*<value>[\s\S]*?</value>)?'
        r'(?:\s*<query>[\s\S]*?</query>)?'
        r'(?:\s*<code>[\s\S]*?</code>)?'
        r'(?:\s*<subcommand>[\s\S]*?</subcommand>)?'
        r'(?:\s*<args>[\s\S]*?</args>)?'
        r'(?:\s*<action>[\s\S]*?</action>)?',
        '',
        text,
    )
    return cleaned.strip()

# ========== AI HELPERS ==========
def build_system_prompt(state: UserState) -> str:
    mode_prompt = MODES.get(state.mode, MODES["agent"])
    extra = ""
    if state.auto_approve:
        extra += "\n[СИСТЕМА] Авто-подтверждение ВКЛЮЧЕНО. Опасные команды выполняются без запроса."
    if state.shell_mode:
        extra += "\n[СИСТЕМА] Режим прямого shell. Всё выполняется мгновенно."
    if ai_memory:
        mem_preview = "\n".join(f"- {k}: {v[:100]}" for k, v in list(ai_memory.items())[:5])
        extra += f"\n[СИСТЕМА] Сохранённые заметки:\n{mem_preview}"
    if state.summary:
        return f"{mode_prompt}\n\nКраткая история диалога:\n{state.summary}{extra}"
    return mode_prompt + extra

def build_payload(state: UserState, user_text: str, image_path: Optional[str] = None):
    provider = state.provider
    system = build_system_prompt(state)

    if provider == "gemini":
        contents = []
        contents.append({
            "role": "user",
            "parts": [{"text": f"[SYSTEM]\n{system}\n[/SYSTEM]\n\nПодтверди, что понял инструкции."}]
        })
        contents.append({"role": "model", "parts": [{"text": "Понял. Буду следовать инструкциям."}]})
        for m in state.messages:
            role = "user" if m["role"] == "user" else "model"
            parts = [{"text": m["content"]}]
            if m.get("image") and role == "user" and os.path.exists(m["image"]):
                try:
                    with open(m["image"], "rb") as f:
                        img_bytes = f.read()
                    parts.append({
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(img_bytes).decode(),
                        }
                    })
                except Exception:
                    pass
            contents.append({"role": role, "parts": parts})
        parts = [{"text": user_text}]
        if image_path and os.path.exists(image_path):
            try:
                with open(image_path, "rb") as f:
                    img_bytes = f.read()
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(img_bytes).decode(),
                    }
                })
            except Exception as e:
                logger.error(f"Image encode error: {e}")
        contents.append({"role": "user", "parts": parts})
        return {"contents": contents}
    else:
        msgs = [{"role": "system", "content": system}]
        for m in state.messages:
            content = m["content"]
            if m.get("image"):
                content += f"\n[Изображение: {m['image']}]"
            msgs.append({"role": m["role"], "content": content})
        current = user_text
        if image_path:
            current += f"\n[Изображение: {image_path}]"
        msgs.append({"role": "user", "content": current})
        return {
            "model": state.model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4096,
        }

def parse_response(provider: str, response_json: dict) -> str:
    parser = PROVIDERS[provider]["parser"]
    try:
        if parser == "gemini":
            return response_json["candidates"][0]["content"]["parts"][0]["text"]
        elif parser == "openai":
            return response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        return f"[Ошибка парсинга: {e}]\nRaw: {json.dumps(response_json, ensure_ascii=False)[:500]}"

def ask_ai(state: UserState, user_text: str, image_path: Optional[str] = None) -> str:
    provider = state.provider
    if provider not in PROVIDERS:
        return "[Ошибка: неизвестный провайдер]"

    key = API_KEYS.get(provider, "")
    if not key:
        return f"[Ошибка: API ключ для {provider} не задан в .env]"

    cfg = PROVIDERS[provider]
    url = cfg["url"].format(model=state.model, key=key)
    headers = {}
    for k, v in cfg["headers"].items():
        if isinstance(v, str) and "{key}" in v:
            headers[k] = v.format(key=key)
        else:
            headers[k] = v

    payload = build_payload(state, user_text, image_path)

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            return f"[HTTP {resp.status_code}] {resp.text[:400]}"
        data = resp.json()
        return parse_response(provider, data)
    except requests.exceptions.Timeout:
        return "[Ошибка: таймаут запроса к AI]"
    except Exception as e:
        return f"[Ошибка запроса: {e}]"

def summarize_context(state: UserState) -> Tuple[bool, str]:
    if not state.messages:
        return True, state.summary

    total_pairs = len(state.messages) // 2
    pairs_to_summarize = max(0, total_pairs - KEEP_AFTER_SUMMARY)

    if pairs_to_summarize <= 0:
        return True, state.summary

    msgs_to_summarize = state.messages[:pairs_to_summarize * 2]
    keep_msgs = state.messages[pairs_to_summarize * 2:]

    dialog_text = ""
    for m in msgs_to_summarize:
        r = "Пользователь" if m["role"] == "user" else "Ассистент"
        dialog_text += f"{r}: {m['content']}\n"

    summary_prompt = (
        "Проанализируй диалог и создай максимально краткую суммаризацию "
        "(ключевые факты, решения, контекст), которую можно использовать как системный промпт. "
        "Только суть, без воды:\n\n"
    )

    old_mode = state.mode
    old_messages = state.messages[:]
    old_summary = state.summary

    state.mode = "detailed"
    state.messages = msgs_to_summarize

    try:
        summary = ask_ai(state, summary_prompt + dialog_text)
        if summary.startswith("[") and ("Ошибка" in summary or "HTTP" in summary):
            raise RuntimeError(summary)
    except Exception as e:
        state.mode = old_mode
        state.messages = old_messages
        state.summary = old_summary
        logger.error(f"Summarize failed: {e}")
        return False, str(e)

    state.mode = old_mode
    state.messages = keep_msgs

    if old_summary:
        summary = f"{old_summary}\n\n[Новая суммаризация]:\n{summary}"

    state.summary = summary
    save_summary(summary)
    return True, summary

# ========== SAFE SEND ==========
def escape_markdown(text: str) -> str:
    """Экранирует спецсимволы MarkdownV2."""
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for ch in chars:
        text = text.replace(ch, f"\\{ch}")
    return text

def split_text(text: str, max_len: int = 4000) -> List[str]:
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    in_code = False
    code_lang = ""

    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                code_lang = stripped[3:].strip()
            in_code = not in_code

        candidate = (current + "\n" + line) if current else line

        if len(candidate) > max_len:
            if in_code and not stripped.startswith("```"):
                chunks.append(current + "\n```")
                current = f"```{code_lang}\n" + line if code_lang else f"```\n" + line
                in_code = True
            else:
                if current:
                    chunks.append(current)
                current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks

def safe_send(chat_id, text: str, reply_to_message_id=None, parse_mode="Markdown"):
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        try:
            kwargs = {"parse_mode": parse_mode}
            if i == 0 and reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            bot.send_message(chat_id, chunk, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            error_text = str(e).lower()
            if "can't parse entities" in error_text or "markdown" in error_text:
                kwargs.pop("parse_mode", None)
                if i == 0 and reply_to_message_id:
                    bot.send_message(chat_id, chunk, reply_to_message_id=reply_to_message_id)
                else:
                    bot.send_message(chat_id, chunk)
            else:
                raise

# ========== KEYBOARDS ==========
def get_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(
        KeyboardButton("🤖 AI"),
        KeyboardButton("💻 Shell"),
        KeyboardButton("⚙️ Настройки"),
        KeyboardButton("🗑 Сброс"),
        KeyboardButton("📋 Статус"),
        KeyboardButton("❓ Помощь"),
        KeyboardButton("📁 Файлы"),
        KeyboardButton("🔋 Termux"),
        KeyboardButton("💾 Сессия"),
    )
    return markup

def get_settings_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔄 Режим", callback_data="settings:mode"),
        InlineKeyboardButton("🧠 Модель", callback_data="settings:model"),
        InlineKeyboardButton("🏭 Провайдер", callback_data="settings:provider"),
        InlineKeyboardButton("🔧 Shell", callback_data="settings:shell"),
        InlineKeyboardButton("⚡ Авто", callback_data="settings:auto"),
        InlineKeyboardButton("📁 Workspace", callback_data="settings:workspace"),
        InlineKeyboardButton("📤 Экспорт", callback_data="settings:export"),
        InlineKeyboardButton("📥 Импорт", callback_data="settings:import"),
        InlineKeyboardButton("🔗 Алиасы", callback_data="settings:aliases"),
        InlineKeyboardButton("🔙 Назад", callback_data="settings:back"),
    )
    return markup

def get_termux_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔋 Батарея", callback_data="termux:battery"),
        InlineKeyboardButton("📶 WiFi", callback_data="termux:wifi"),
        InlineKeyboardButton("🔦 Фонарик", callback_data="termux:torch"),
        InlineKeyboardButton("📍 Локация", callback_data="termux:location"),
        InlineKeyboardButton("📋 Буфер обмена", callback_data="termux:clipboard"),
        InlineKeyboardButton("🔊 TTS", callback_data="termux:tts"),
        InlineKeyboardButton("📳 Вибрация", callback_data="termux:vibrate"),
        InlineKeyboardButton("🔔 Уведомление", callback_data="termux:notify"),
    )
    return markup

# ========== BOT ==========
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен.")
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        shell_status = "🟢 ON" if state.shell_mode else "🔴 OFF"
        auto_status = "🟢 ON" if state.auto_approve else "🔴 OFF"
        termux_status = "🟢 API" if TERMUX_API_PKG else "🔴 Нет API"
        bot.reply_to(
            message,
            f"🤖 *Termux AI Bot v8* — Ultimate Edition\n"
            f"Провайдер: `{state.provider}` | Модель: `{state.model}`\n"
            f"Режим: `{state.mode}` | Shell: {shell_status} | Auto: {auto_status}\n"
            f"Termux: {termux_status} | Workspace: `{WORKSPACE}`\n"
            f"Сессия: `{state.current_session}` | Контекст: `{len(state.messages)//2}` пар\n\n"
            f"*Быстрые префиксы:*\n"
            f"`!команда` — выполнить в shell без AI\n"
            f"`?вопрос` — быстрый вопрос (игнорирует режим shell)\n"
            f"`@алиас` — выполнить алиас\n\n"
            f"Используй кнопки ниже или /help",
            reply_markup=get_main_keyboard(),
        )

@bot.message_handler(commands=["help"])
def cmd_help(message):
    if message.from_user.id != ADMIN_ID:
        return
    help_text = (
        "📋 *Команды:*\n"
        "/start — статус\n"
        "/settings — единое меню настроек\n"
        "/mode — режим ответа\n"
        "/model — модель\n"
        "/provider — провайдер\n"
        "/shell — переключить прямой режим shell\n"
        "/auto — авто-подтверждение опасных действий\n"
        "/workspace — сменить рабочую папку\n"
        "/reset — сброс контекста\n"
        "/status — настройки\n"
        "/summary — суммаризация\n"
        "/undo — отменить последнее действие\n"
        "/calc — калькулятор\n"
        "/note — быстрая заметка\n"
        "/remind — напоминание (минуты|текст)\n"
        "/tldr — краткое содержание файла\n"
        "/diff — изменения в workspace (git diff)\n"
        "/export — экспорт контекста\n"
        "/import — импорт контекста\n"
        "/add_provider — добавить провайдера\n"
        "/add_model — добавить модель\n"
        "/session — управление сессиями\n"
        "/alias — управление алиасами\n"
        "/sendfile — отправить файл из workspace\n"
        "/termux — меню Termux API\n"
        "/pkg — управление пакетами\n"
        "/backup — бэкап конфигурации\n"
        "/voice — озвучить текст (TTS)\n"
        "/help — справка\n\n"
        "*Префиксы:*\n"
        "`!команда` — shell без AI\n"
        "`?вопрос` — вопрос к AI в любом режиме\n"
        "`@алиас` — выполнить алиас\n\n"
        "*Режим agent:* AI использует инструменты через теги `<tool>`\n"
        "Все опасные действия требуют подтверждения (если /auto выключен)."
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "⚙️ *Меню настроек:*", reply_markup=get_settings_keyboard())

@bot.callback_query_handler(func=lambda call: call.data.startswith("settings:"))
def callback_settings(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    action = call.data.split(":")[1]
    if action == "mode":
        cmd_mode(call.message)
    elif action == "model":
        cmd_model(call.message)
    elif action == "provider":
        cmd_provider(call.message)
    elif action == "shell":
        toggle_shell(call.message)
    elif action == "auto":
        toggle_auto(call.message)
    elif action == "workspace":
        cmd_workspace(call.message)
    elif action == "export":
        cmd_export(call.message)
    elif action == "import":
        cmd_import(call.message)
    elif action == "aliases":
        cmd_alias_list(call.message)
    elif action == "back":
        bot.edit_message_text("⚙️ Меню закрыто. Используй /settings", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "Готово")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        shell_status = "🟢 ON" if state.shell_mode else "🔴 OFF"
        auto_status = "🟢 ON" if state.auto_approve else "🔴 OFF"
        summary_preview = state.summary[:200] + "..." if len(state.summary) > 200 else state.summary or "(пусто)"
        approx_tokens = sum(len(m["content"]) for m in state.messages) // 4
        mem_count = len(ai_memory)
        alias_count = len(command_aliases)
        sessions = ", ".join(list_sessions()[:5]) or "(нет)"
        bot.reply_to(
            message,
            f"⚙️ *Статус:*\n"
            f"Провайдер: `{state.provider}` | Модель: `{state.model}`\n"
            f"Режим: `{state.mode}` | Сессия: `{state.current_session}`\n"
            f"Shell: {shell_status} | Auto-approve: {auto_status}\n"
            f"Workspace: `{WORKSPACE}`\n"
            f"Сообщений: `{len(state.messages)}` (пар: `{len(state.messages)//2}`)\n"
            f"Примерно токенов: `{approx_tokens}`\n"
            f"До суммаризации: `{max(0, CONTEXT_LIMIT - len(state.messages)//2)}` пар\n"
            f"Заметок: `{mem_count}` | Алиасов: `{alias_count}`\n"
            f"Сессии: `{sessions}`\n"
            f"Суммаризация: `{summary_preview}`",
        )

@bot.message_handler(commands=["summary"])
def cmd_summary(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        if not state.summary:
            bot.reply_to(message, "📭 Суммаризация пуста.")
            return
        text = f"📝 *Текущая суммаризация:*\n\n{state.summary}"
        safe_send(message.chat.id, text)

@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if message.from_user.id != ADMIN_ID:
        return
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Да, сбросить", callback_data="reset:confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="reset:cancel")
    )
    bot.reply_to(message, "⚠️ Сбросить контекст и суммаризацию?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reset:"))
def callback_reset(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    action = call.data.split(":")[1]
    state = get_state(ADMIN_ID)
    if action == "confirm":
        with state.lock:
            state.messages = []
            state.summary = ""
            state.action_history = []
            if os.path.exists(CONTEXT_FILE):
                try:
                    os.remove(CONTEXT_FILE)
                except Exception:
                    pass
        bot.answer_callback_query(call.id, "Контекст сброшен")
        bot.edit_message_text("🗑 Контекст, суммаризация и история сброшены.", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "Отменено")
        bot.edit_message_text("❌ Сброс отменен.", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["mode"])
def cmd_mode(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    markup = InlineKeyboardMarkup(row_width=2)
    for k in MODES.keys():
        label = f"{'✅ ' if state.mode == k else ''}{k}"
        markup.add(InlineKeyboardButton(label, callback_data=f"mode:{k}"))
    bot.reply_to(message, "Выбери режим:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode:"))
def callback_mode(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    mode = call.data.split(":")[1]
    state = get_state(ADMIN_ID)
    with state.lock:
        state.mode = mode
    bot.answer_callback_query(call.id, f"Режим: {mode}")
    bot.edit_message_text(f"✅ Режим: `{mode}`", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.message_handler(commands=["provider"])
def cmd_provider(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    markup = InlineKeyboardMarkup(row_width=1)
    for k in PROVIDERS.keys():
        label = f"{'✅ ' if state.provider == k else ''}{k}"
        markup.add(InlineKeyboardButton(label, callback_data=f"provider:{k}"))
    bot.reply_to(message, "Выбери провайдера:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("provider:"))
def callback_provider(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    provider = call.data.split(":")[1]
    state = get_state(ADMIN_ID)
    with state.lock:
        state.provider = provider
        models = PROVIDER_MODELS.get(provider, [""])
        state.model = models[0] if models else ""
    bot.answer_callback_query(call.id, f"Провайдер: {provider}")

    markup = InlineKeyboardMarkup(row_width=1)
    for m in PROVIDER_MODELS.get(provider, []):
        label = f"{'✅ ' if state.model == m else ''}{m}"
        markup.add(InlineKeyboardButton(label, callback_data=f"model:{m}"))
    if not markup.keyboard:
        bot.send_message(call.message.chat.id, f"✅ Провайдер: `{provider}`\nНет моделей.", parse_mode="Markdown")
    else:
        bot.send_message(call.message.chat.id, f"✅ Провайдер: `{provider}`\nВыбери модель:", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def callback_model(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    model = call.data.split(":", 1)[1]
    state = get_state(ADMIN_ID)
    with state.lock:
        state.model = model
    bot.answer_callback_query(call.id, f"Модель: {model}")
    bot.edit_message_text(f"✅ Модель: `{model}`", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.message_handler(commands=["model"])
def cmd_model(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    markup = InlineKeyboardMarkup(row_width=1)
    for m in PROVIDER_MODELS.get(state.provider, []):
        label = f"{'✅ ' if state.model == m else ''}{m}"
        markup.add(InlineKeyboardButton(label, callback_data=f"model:{m}"))
    if not markup.keyboard:
        bot.reply_to(message, f"Текущая: `{state.model}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"Текущая: `{state.model}`\nВыбери:", reply_markup=markup, parse_mode="Markdown")

# ========== SHELL MODE ==========
def toggle_shell(message):
    state = get_state(ADMIN_ID)
    with state.lock:
        state.shell_mode = not state.shell_mode
        status = "🟢 ВКЛЮЧЕН" if state.shell_mode else "🔴 ВЫКЛЮЧЕН"
    bot.reply_to(message, f"🔧 Режим прямого shell: {status}\n\nВ этом режиме всё что ты пишешь выполняется как bash-команда.\nИспользуй `?текст` чтобы отправить вопрос AI.")

@bot.message_handler(commands=["shell"])
def cmd_shell(message):
    if message.from_user.id != ADMIN_ID:
        return
    toggle_shell(message)

# ========== AUTO APPROVE ==========
def toggle_auto(message):
    state = get_state(ADMIN_ID)
    with state.lock:
        state.auto_approve = not state.auto_approve
        status = "🟢 ВКЛЮЧЕНО" if state.auto_approve else "🔴 ВЫКЛЮЧЕНО"
    bot.reply_to(message, f"⚡ Авто-подтверждение опасных действий: {status}\n\nВНИМАНИЕ: AI сможет выполнять команды и писать файлы без твоего разрешения.")

@bot.message_handler(commands=["auto"])
def cmd_auto(message):
    if message.from_user.id != ADMIN_ID:
        return
    toggle_auto(message)

# ========== WORKSPACE ==========
@bot.message_handler(commands=["workspace"])
def cmd_workspace(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, f"Текущая рабочая папка:\n`{WORKSPACE}`\n\nОтправь новый путь (абсолютный) ответным сообщением:")
    bot.register_next_step_handler(msg, process_workspace_step)

def process_workspace_step(message):
    global WORKSPACE
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    new_path = message.text.strip()
    if not new_path.startswith("/"):
        new_path = os.path.join(os.path.expanduser("~"), new_path)
    try:
        os.makedirs(new_path, exist_ok=True)
        WORKSPACE = os.path.abspath(new_path)
        bot.reply_to(message, f"✅ Рабочая папка изменена:\n`{WORKSPACE}`")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

# ========== CALC ==========
@bot.message_handler(commands=["calc"])
def cmd_calc(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь математическое выражение для вычисления:")
    bot.register_next_step_handler(msg, process_calc_step)

def process_calc_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    expr = message.text.strip()
    try:
        allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith('_')}
        allowed.update({"abs": abs, "round": round, "max": max, "min": min, "sum": sum, "pow": pow})
        # Безопасный eval через ast можно было бы лучше, но пока ограничим
        result = eval(expr, {"__builtins__": {}}, allowed)
        bot.reply_to(message, f"🧮 *Результат:*\n`{expr}` = `{result}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка вычисления: {e}")

# ========== NOTE ==========
@bot.message_handler(commands=["note"])
def cmd_note(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь заметку в формате `ключ|значение` или просто текст для сохранения под случайным ключом:")
    bot.register_next_step_handler(msg, process_note_step)

def process_note_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    text = message.text.strip()
    if "|" in text:
        key, value = text.split("|", 1)
        key = key.strip()
        value = value.strip()
    else:
        key = f"note_{datetime.now().strftime('%H%M%S')}"
        value = text
    result = tool_memory_save(key, value)
    bot.reply_to(message, f"📝 {result}\nКлюч: `{key}`", parse_mode="Markdown")

# ========== REMIND ==========
@bot.message_handler(commands=["remind"])
def cmd_remind(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь напоминание в формате:\n`минуты|текст`\n\n*Пример:* `5|Проверить логи`", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_remind_step)

def process_remind_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        minutes_str, text = message.text.split("|", 1)
        minutes = int(minutes_str.strip())
        if minutes <= 0:
            raise ValueError("Минуты должны быть > 0")
        chat_id = message.chat.id
        def reminder():
            try:
                bot.send_message(chat_id, f"⏰ *Напоминание:*\n{text}", parse_mode="Markdown")
            except Exception:
                pass
            # Удаляем таймер из списка
            state = get_state(ADMIN_ID)
            with state.lock:
                state.reminders = [t for t in state.reminders if t.is_alive()]
        timer = threading.Timer(minutes * 60, reminder)
        timer.start()
        state = get_state(ADMIN_ID)
        with state.lock:
            state.reminders.append(timer)
        bot.reply_to(message, f"⏰ Напоминание установлено через {minutes} мин.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}\nФормат: `минуты|текст`")

# ========== TLDR ==========
@bot.message_handler(commands=["tldr"])
def cmd_tldr(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь путь к файлу для краткого содержания:")
    bot.register_next_step_handler(msg, process_tldr_step)

def process_tldr_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    path = message.text.strip()
    content = tool_read_file(path)
    if content.startswith("["):
        bot.reply_to(message, f"❌ {content}")
        return
    prompt = f"Сделай краткое содержание (TL;DR) этого файла. Только ключевые моменты:\n\n```\n{content[:6000]}\n```"
    state = get_state(ADMIN_ID)
    result = ask_ai(state, prompt)
    safe_send(message.chat.id, f"📄 *TL;DR для `{path}`:*\n\n{result}")

# ========== DIFF ==========
@bot.message_handler(commands=["diff"])
def cmd_diff(message):
    if message.from_user.id != ADMIN_ID:
        return
    result = tool_git("diff")
    preview = result[:4000] + "..." if len(result) > 4000 else result
    safe_send(message.chat.id, f"📊 *Git diff:*\n```\n{preview}\n```")

# ========== EXPORT / IMPORT ==========
@bot.message_handler(commands=["export"])
def cmd_export(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        data = {
            "messages": state.messages,
            "summary": state.summary,
            "provider": state.provider,
            "model": state.model,
            "mode": state.mode,
            "exported_at": datetime.now().isoformat(),
        }
    export_path = os.path.join(WORKSPACE, f"context_export_{int(time.time())}.json")
    try:
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with open(export_path, "rb") as f:
            bot.send_document(message.chat.id, InputFile(f, os.path.basename(export_path)), caption=f"📤 Экспорт контекста\nПар: {len(state.messages)//2}")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка экспорта: {e}")

@bot.message_handler(commands=["import"])
def cmd_import(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь JSON-файл экспорта контекста ответным сообщением (как документ).")
    bot.register_next_step_handler(msg, process_import_step)

def process_import_step(message):
    if not message.document:
        bot.reply_to(message, "❌ Нужно отправить файл документом.")
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        data = json.loads(downloaded.decode("utf-8"))
        state = get_state(ADMIN_ID)
        with state.lock:
            state.messages = data.get("messages", [])
            state.summary = data.get("summary", "")
            state.provider = data.get("provider", DEFAULT_PROVIDER)
            state.model = data.get("model", DEFAULT_MODEL)
            state.mode = data.get("mode", DEFAULT_MODE)
        bot.reply_to(message, f"✅ Контекст импортирован!\nПар: {len(state.messages)//2}\nПровайдер: `{state.provider}`\nМодель: `{state.model}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка импорта: {e}")

# ========== UNDO ==========
@bot.message_handler(commands=["undo"])
def cmd_undo(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        if not state.action_history:
            bot.reply_to(message, "📭 История действий пуста.")
            return
        last = state.action_history.pop()

    action_type = last.get("type")
    if action_type == "write_file":
        path = last.get("path")
        backup = last.get("backup")
        if backup is not None:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(backup)
                bot.reply_to(message, f"↩️ Файл восстановлен:\n`{path}`", parse_mode="Markdown")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка восстановления: {e}")
        else:
            try:
                os.remove(path)
                bot.reply_to(message, f"🗑 Файл удалён (был создан):\n`{path}`", parse_mode="Markdown")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
    elif action_type == "execute_command":
        bot.reply_to(message, f"ℹ️ Команда уже выполнена, отменить невозможно.\nКоманда: `{last.get('command')}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, "ℹ️ Неизвестное действие.")

# ========== ADD PROVIDER ==========
@bot.message_handler(commands=["add_provider"])
def cmd_add_provider(message):
    if message.from_user.id != ADMIN_ID:
        return
    text = (
        "Отправь данные нового провайдера ОДНИМ сообщением в формате:\n\n"
        "`name|url_template|parser|header1:value1,header2:value2`\n\n"
        "*Пример:*\n"
        "`myapi|https://api.myai.com/v1/chat?key={key}|openai|Authorization:Bearer {key},Content-Type:application/json`\n\n"
        "*parser:* `openai` или `gemini`"
    )
    msg = bot.reply_to(message, text, parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_add_provider_step)

def process_add_provider_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        parts = message.text.split("|")
        if len(parts) != 4:
            raise ValueError("Нужно 4 части, разделённые |")
        name, url, parser, headers_str = parts
        headers = {}
        if headers_str.strip():
            for h in headers_str.split(","):
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        PROVIDERS[name.strip()] = {
            "url": url.strip(),
            "headers": headers,
            "parser": parser.strip(),
            "vision": False,
        }
        if name.strip() not in PROVIDER_MODELS:
            PROVIDER_MODELS[name.strip()] = []
        save_custom_config()
        bot.reply_to(message, f"✅ Провайдер `{name}` добавлен!\nТеперь добавь модель через /add_model")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}\nПроверь формат.")

# ========== ADD MODEL ==========
@bot.message_handler(commands=["add_model"])
def cmd_add_model(message):
    if message.from_user.id != ADMIN_ID:
        return
    providers_list = ", ".join(f"`{k}`" for k in PROVIDERS.keys())
    msg = bot.reply_to(message, f"Отправь: `провайдер|модель`\n\nДоступные провайдеры: {providers_list}", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_add_model_step)

def process_add_model_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        prov, model = message.text.split("|", 1)
        prov = prov.strip()
        model = model.strip()
        if prov not in PROVIDERS:
            raise ValueError(f"Провайдер {prov} не найден. Сначала /add_provider")
        if prov not in PROVIDER_MODELS:
            PROVIDER_MODELS[prov] = []
        if model not in PROVIDER_MODELS[prov]:
            PROVIDER_MODELS[prov].append(model)
        save_custom_config()
        bot.reply_to(message, f"✅ Модель `{model}` добавлена к провайдеру `{prov}`")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

# ========== SESSIONS ==========
@bot.message_handler(commands=["session"])
def cmd_session(message):
    if message.from_user.id != ADMIN_ID:
        return
    sessions = list_sessions()
    text = "💾 *Управление сессиями*\n\n"
    if sessions:
        text += "Сохранённые сессии:\n" + "\n".join(f"• `{s}`" for s in sessions) + "\n\n"
    else:
        text += "Нет сохранённых сессий.\n\n"
    text += "Отправь действие:\n`save|имя` — сохранить\n`load|имя` — загрузить\n`delete|имя` — удалить"
    msg = bot.reply_to(message, text, parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_session_step)

def process_session_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        action, name = message.text.split("|", 1)
        action = action.strip().lower()
        name = name.strip()
        state = get_state(ADMIN_ID)
        if action == "save":
            ok, res = save_session(state, name)
            bot.reply_to(message, f"✅ Сессия `{name}` сохранена!\n`{res}`" if ok else f"❌ {res}")
        elif action == "load":
            ok, res = load_session(state, name)
            bot.reply_to(message, f"✅ {res}" if ok else f"❌ {res}")
        elif action == "delete":
            path = os.path.join(SESSIONS_DIR, f"{name}.json")
            if os.path.exists(path):
                os.remove(path)
                bot.reply_to(message, f"🗑 Сессия `{name}` удалена.")
            else:
                bot.reply_to(message, f"❌ Сессия `{name}` не найдена.")
        else:
            bot.reply_to(message, "❌ Неизвестное действие. Используй save|имя, load|имя или delete|имя")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

# ========== ALIASES ==========
@bot.message_handler(commands=["alias"])
def cmd_alias(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not command_aliases:
        text = "🔗 *Алиасы:* (пусто)\n\n"
    else:
        text = "🔗 *Алиасы:*\n" + "\n".join(f"`{k}` → `{v}`" for k, v in command_aliases.items()) + "\n\n"
    text += "Отправь: `add|алиас|команда` или `del|алиас`"
    msg = bot.reply_to(message, text, parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_alias_step)

def process_alias_step(message):
    global command_aliases
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        parts = message.text.split("|")
        if len(parts) == 3 and parts[0].strip().lower() == "add":
            alias, cmd = parts[1].strip(), parts[2].strip()
            command_aliases[alias] = cmd
            save_aliases(command_aliases)
            save_custom_config()
            bot.reply_to(message, f"✅ Алиас `{alias}` добавлен → `{cmd}`")
        elif len(parts) == 2 and parts[0].strip().lower() == "del":
            alias = parts[1].strip()
            if alias in command_aliases:
                del command_aliases[alias]
                save_aliases(command_aliases)
                save_custom_config()
                bot.reply_to(message, f"🗑 Алиас `{alias}` удалён.")
            else:
                bot.reply_to(message, f"❌ Алиас `{alias}` не найден.")
        else:
            bot.reply_to(message, "❌ Формат: `add|алиас|команда` или `del|алиас`")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

def cmd_alias_list(message):
    if not command_aliases:
        bot.reply_to(message, "🔗 Алиасы: (пусто)\n\nИспользуй /alias")
    else:
        text = "🔗 *Алиасы:*\n" + "\n".join(f"`{k}` → `{v}`" for k, v in command_aliases.items())
        bot.reply_to(message, text, parse_mode="Markdown")

# ========== SEND FILE ==========
@bot.message_handler(commands=["sendfile"])
def cmd_sendfile(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь путь к файлу в workspace для отправки в Telegram:")
    bot.register_next_step_handler(msg, process_sendfile_step)

def process_sendfile_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    path = message.text.strip()
    safe, full = is_path_safe(path)
    if not safe:
        bot.reply_to(message, f"❌ {full}")
        return
    if not os.path.exists(full):
        bot.reply_to(message, f"❌ Файл не найден: `{full}`", parse_mode="Markdown")
        return
    try:
        with open(full, "rb") as f:
            bot.send_document(message.chat.id, InputFile(f, os.path.basename(full)), caption=f"📁 `{os.path.basename(full)}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка отправки: {e}")

# ========== TERMUX MENU ==========
@bot.message_handler(commands=["termux"])
def cmd_termux(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not TERMUX_API_PKG:
        bot.reply_to(message, "⚠️ Termux API не установлен. Установи: `pkg install termux-api`", parse_mode="Markdown")
        return
    bot.reply_to(message, "🔋 *Termux API меню:*", reply_markup=get_termux_keyboard())

@bot.callback_query_handler(func=lambda call: call.data.startswith("termux:"))
def callback_termux(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return
    action = call.data.split(":")[1]
    state = get_state(ADMIN_ID)

    if action == "battery":
        result = termux_battery()
    elif action == "wifi":
        result = termux_wifi()
    elif action == "torch":
        result = termux_torch("on")
    elif action == "location":
        result = termux_location()
    elif action == "clipboard":
        result = termux_clipboard_get()
    elif action == "tts":
        bot.answer_callback_query(call.id, "Отправь /voice текст")
        bot.edit_message_text("🔊 Отправь /voice текст для озвучки", call.message.chat.id, call.message.message_id)
        return
    elif action == "vibrate":
        result = termux_vibrate(500)
    elif action == "notify":
        bot.answer_callback_query(call.id, "Отправь текст уведомления")
        msg = bot.send_message(call.message.chat.id, "Отправь текст для уведомления:")
        bot.register_next_step_handler(msg, lambda m: bot.reply_to(m, termux_notification("Termux Bot", m.text)))
        return
    else:
        result = "[Неизвестное действие]"

    bot.answer_callback_query(call.id, "Готово")
    bot.edit_message_text(result, call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["voice"])
def cmd_voice(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Отправь текст для озвучки (TTS):")
    bot.register_next_step_handler(msg, process_voice_step)

def process_voice_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    result = termux_tts(message.text.strip())
    bot.reply_to(message, result)

# ========== PKG ==========
@bot.message_handler(commands=["pkg"])
def cmd_pkg(message):
    if message.from_user.id != ADMIN_ID:
        return
    text = (
        "📦 *Управление пакетами Termux*\n\n"
        "Отправь действие:\n"
        "`install|пакет` — установить\n"
        "`remove|пакет` — удалить\n"
        "`search|запрос` — поиск\n"
        "`update` — обновить списки\n"
        "`upgrade` — обновить пакеты\n"
        "`list` — список установленных\n"
        "`clean` — очистить кэш"
    )
    msg = bot.reply_to(message, text, parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_pkg_step)

def process_pkg_step(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Отменено.")
        return
    try:
        parts = message.text.split("|", 1)
        action = parts[0].strip()
        args = parts[1].strip() if len(parts) > 1 else ""
        result = tool_pkg(action, args)
        safe_send(message.chat.id, f"📦 *pkg {action}:*\n```\n{result[:3000]}\n```")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

# ========== BACKUP ==========
@bot.message_handler(commands=["backup"])
def cmd_backup(message):
    if message.from_user.id != ADMIN_ID:
        return
    state = get_state(ADMIN_ID)
    with state.lock:
        data = {
            "messages": state.messages,
            "summary": state.summary,
            "provider": state.provider,
            "model": state.model,
            "mode": state.mode,
            "memory": ai_memory,
            "aliases": command_aliases,
            "workspace": WORKSPACE,
            "backed_up_at": datetime.now().isoformat(),
        }
    backup_path = os.path.join(WORKSPACE, f"backup_{int(time.time())}.json")
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with open(backup_path, "rb") as f:
            bot.send_document(message.chat.id, InputFile(f, os.path.basename(backup_path)), caption="💾 Бэкап конфигурации")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка бэкапа: {e}")

# ========== TOOL CONFIRMATION HANDLER ==========
pending_actions: Dict[str, Dict[str, Any]] = {}
pending_counter = 0
pending_lock = threading.Lock()

def add_pending(action: dict) -> str:
    global pending_counter
    with pending_lock:
        pending_counter += 1
        aid = f"act{pending_counter}"
        pending_actions[aid] = action
        # Очистка старых записей
        now = time.time()
        to_remove = [k for k, v in pending_actions.items() if v.get("time", now) < now - PENDING_MAX_AGE]
        for k in to_remove:
            del pending_actions[k]
        action["time"] = now
        action["status"] = "pending"
        return aid

@bot.callback_query_handler(func=lambda call: call.data.startswith("tool:"))
def callback_tool(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔")
        return

    parts = call.data.split(":", 3)
    if len(parts) < 3:
        return
    _, action_id, decision = parts[0], parts[1], parts[2]

    pending = pending_actions.get(action_id)
    if not pending:
        bot.answer_callback_query(call.id, "Устарело")
        return

    if pending.get("status") != "pending":
        bot.answer_callback_query(call.id, "Уже обработано")
        return

    if decision == "cancel":
        pending["status"] = "cancelled"
        bot.answer_callback_query(call.id, "Отменено")
        bot.edit_message_text("❌ Действие отменено.", call.message.chat.id, call.message.message_id)
        return

    if decision == "exec":
        tool = pending["tool"]
        result = ""
        state = get_state(ADMIN_ID)
        try:
            if tool == "execute_command":
                result = tool_execute_command(pending["command"])
                with state.lock:
                    state.action_history.append({"type": "execute_command", "command": pending["command"]})
            elif tool == "write_file":
                full_path = resolve_path(pending["path"])
                backup = None
                if os.path.exists(full_path):
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        backup = f.read()
                result = tool_write_file(pending["path"], pending["content"])
                with state.lock:
                    state.action_history.append({"type": "write_file", "path": full_path, "backup": backup})
            elif tool == "edit_file":
                full_path = resolve_path(pending["path"])
                backup = None
                if os.path.exists(full_path):
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        backup = f.read()
                result = tool_edit_file(pending["path"], pending["old_string"], pending["new_string"])
                with state.lock:
                    state.action_history.append({"type": "write_file", "path": full_path, "backup": backup})
            elif tool == "delete_file":
                result = tool_delete_file(pending["path"])
            elif tool == "read_file":
                result = tool_read_file(pending["path"])
            elif tool == "list_directory":
                result = tool_list_directory(pending["path"])
            elif tool == "search":
                result = tool_search(pending["path"], pending.get("regex", ""))
            elif tool == "memory_save":
                result = tool_memory_save(pending["key"], pending["value"])
            elif tool == "memory_read":
                result = tool_memory_read(pending["key"])
            elif tool == "web_search":
                result = tool_web_search(pending["query"])
            elif tool == "python":
                result = tool_python(pending["code"])
            elif tool == "git":
                result = tool_git(pending["subcommand"], pending.get("args", ""))
            elif tool == "termux":
                result = tool_termux(pending.get("action", ""), pending.get("args", ""))
            elif tool == "pkg":
                result = tool_pkg(pending.get("action", ""), pending.get("args", ""))
            else:
                result = f"[Неизвестный инструмент: {tool}]"
        except Exception as e:
            result = f"[Ошибка: {e}]"

        pending["status"] = "done"
        pending["result"] = result

        preview = result[:3000] + "..." if len(result) > 3000 else result
        text = f"✅ *Выполнено:* `{tool}`\n\n```\n{preview}\n```"
        bot.answer_callback_query(call.id, "Выполнено")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")

# ========== MAIN HANDLER ==========
@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "document"])
def handle_message(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен.")
        return

    state = get_state(ADMIN_ID)
    image_path = None
    document_path = None

    # Обработка фото
    if message.content_type == "photo":
        if not PROVIDERS.get(state.provider, {}).get("vision", False):
            bot.reply_to(message, "⚠️ Текущий провайдер не поддерживает изображения. Используй Gemini или OpenRouter с vision-моделью.")
            return
        try:
            file_id = message.photo[-1].file_id
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            image_path = os.path.join(WORKSPACE, f"photo_{int(time.time())}.jpg")
            with open(image_path, "wb") as f:
                f.write(downloaded)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка загрузки фото: {e}")
            return
        text = message.caption.strip() if message.caption else "Опиши что на изображении."
    # Обработка документов
    elif message.content_type == "document":
        try:
            file_id = message.document.file_id
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            ext = os.path.splitext(message.document.file_name)[1] or ".bin"
            document_path = os.path.join(WORKSPACE, f"doc_{int(time.time())}{ext}")
            with open(document_path, "wb") as f:
                f.write(downloaded)
            # Читаем текстовые файлы для отправки в AI
            text_content = ""
            if ext.lower() in (".txt", ".py", ".md", ".json", ".sh", ".csv", ".log", ".yaml", ".yml", ".xml", ".html", ".css", ".js", ".cpp", ".c", ".h", ".java", ".go", ".rs", ".ts"):
                try:
                    with open(document_path, "r", encoding="utf-8", errors="replace") as f:
                        text_content = f.read()
                    if len(text_content) > 6000:
                        text_content = text_content[:6000] + "\n\n... [обрезано]"
                except Exception:
                    pass
            if message.caption:
                text = f"{message.caption.strip()}\n\n[Файл: {message.document.file_name}]\n```\n{text_content}\n```"
            else:
                text = f"Проанализируй файл: {message.document.file_name}\n```\n{text_content}\n```"
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка загрузки документа: {e}")
            return
    else:
        text = message.text

    # Префикс ! — прямой shell
    if text.startswith("!"):
        command = text[1:].strip()
        result = tool_execute_command(command)
        preview = result[:4000] + "..." if len(result) > 4000 else result
        safe_send(message.chat.id, f"💻 *Shell:* `{command}`\n\n```\n{preview}\n```", reply_to_message_id=message.message_id)
        return

    # Префикс ? — вопрос к AI даже в shell режиме
    if text.startswith("?"):
        text = text[1:].strip()
    elif text.startswith("@"):
        # Алиас
        alias = text[1:].strip().split()[0]
        if alias in command_aliases:
            result = tool_execute_command(command_aliases[alias])
            preview = result[:4000] + "..." if len(result) > 4000 else result
            safe_send(message.chat.id, f"🔗 *Алиас `{alias}`:*\n\n```\n{preview}\n```", reply_to_message_id=message.message_id)
        else:
            bot.reply_to(message, f"❌ Алиас `{alias}` не найден. Используй /alias")
        return
    elif text.startswith("/"):
        bot.reply_to(message, "❓ Неизвестная команда. /help")
        return

    if len(text) > 4000:
        bot.reply_to(message, "⚠️ Сообщение слишком длинное (>4000 символов).")
        return

    # Reply keyboard shortcuts
    if text in ("🤖 AI", "💻 Shell", "⚙️ Настройки", "🗑 Сброс", "📋 Статус", "❓ Помощь", "📁 Файлы", "🔋 Termux", "💾 Сессия"):
        if text == "🤖 AI":
            with state.lock:
                state.shell_mode = False
            bot.reply_to(message, "🤖 Режим AI включён. Теперь сообщения обрабатываются нейросетью.")
        elif text == "💻 Shell":
            with state.lock:
                state.shell_mode = True
            bot.reply_to(message, "💻 Режим Shell включён. Всё что ты пишешь выполняется как bash-команда.\nИспользуй `?текст` для вопроса AI.")
        elif text == "⚙️ Настройки":
            cmd_settings(message)
        elif text == "🗑 Сброс":
            cmd_reset(message)
        elif text == "📋 Статус":
            cmd_status(message)
        elif text == "❓ Помощь":
            cmd_help(message)
        elif text == "📁 Файлы":
            cmd_sendfile(message)
        elif text == "🔋 Termux":
            cmd_termux(message)
        elif text == "💾 Сессия":
            cmd_session(message)
        return

    # Shell mode — всё выполняется как bash (кроме префикса ?)
    if state.shell_mode and not (message.text and message.text.startswith("?")):
        result = tool_execute_command(text)
        preview = result[:4000] + "..." if len(result) > 4000 else result
        safe_send(message.chat.id, f"💻 *Shell:* `{text}`\n\n```\n{preview}\n```", reply_to_message_id=message.message_id)
        return

    # Normal AI flow
    stop_typing = threading.Event()
    typing_thread = threading.Thread(target=typing_loop, args=(message.chat.id, stop_typing), daemon=True)
    typing_thread.start()

    try:
        with state.lock:
            response = ask_ai(state, text, image_path)
            msg_entry = {"role": "user", "content": text}
            if image_path:
                msg_entry["image"] = image_path
            if document_path:
                msg_entry["document"] = document_path
            state.messages.append(msg_entry)
            state.messages.append({"role": "assistant", "content": response})
            # Исправленная обрезка: сохраняем только четное количество
            if len(state.messages) > MAX_HISTORY_PAIRS * 2:
                state.messages = state.messages[-MAX_HISTORY_PAIRS * 2:]

        # Parse tools
        actions = parse_tools(response)
        safe_tools = ["read_file", "list_directory", "search", "memory_read", "web_search", "python", "git", "termux", "pkg"]
        dangerous_tools = ["execute_command", "write_file", "edit_file", "delete_file", "memory_save"]

        safe_actions = [a for a in actions if a["tool"] in safe_tools]
        dangerous_actions = [a for a in actions if a["tool"] in dangerous_tools]

        # Execute safe tools immediately
        safe_results = []
        for a in safe_actions:
            try:
                if a["tool"] == "read_file":
                    r = tool_read_file(a["path"])
                elif a["tool"] == "list_directory":
                    r = tool_list_directory(a["path"])
                elif a["tool"] == "search":
                    r = tool_search(a["path"], a.get("regex", ""))
                elif a["tool"] == "memory_read":
                    r = tool_memory_read(a["key"])
                elif a["tool"] == "web_search":
                    r = tool_web_search(a["query"])
                elif a["tool"] == "python":
                    r = tool_python(a["code"])
                elif a["tool"] == "git":
                    r = tool_git(a["subcommand"], a.get("args", ""))
                elif a["tool"] == "termux":
                    r = tool_termux(a.get("action", ""), a.get("args", ""))
                elif a["tool"] == "pkg":
                    r = tool_pkg(a.get("action", ""), a.get("args", ""))
                else:
                    r = "[Неизвестный инструмент]"
                safe_results.append(f"🔍 {a['tool']}:\n{r[:1500]}")
            except Exception as e:
                safe_results.append(f"❌ {a['tool']} ошибка: {e}")

        # Clean text for user
        clean_text = strip_tools(response)
        if not clean_text and not actions:
            clean_text = response

        full_reply = clean_text
        if safe_results:
            full_reply += "\n\n" + "\n\n".join(safe_results)

        safe_send(message.chat.id, full_reply, reply_to_message_id=message.message_id)

        # Handle dangerous actions
        if dangerous_actions:
            if state.auto_approve:
                for a in dangerous_actions:
                    try:
                        if a["tool"] == "execute_command":
                            r = tool_execute_command(a["command"])
                            with state.lock:
                                state.action_history.append({"type": "execute_command", "command": a["command"]})
                        elif a["tool"] == "write_file":
                            full_path = resolve_path(a["path"])
                            backup = None
                            if os.path.exists(full_path):
                                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                                    backup = f.read()
                            r = tool_write_file(a["path"], a["content"])
                            with state.lock:
                                state.action_history.append({"type": "write_file", "path": full_path, "backup": backup})
                        elif a["tool"] == "edit_file":
                            full_path = resolve_path(a["path"])
                            backup = None
                            if os.path.exists(full_path):
                                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                                    backup = f.read()
                            r = tool_edit_file(a["path"], a["old_string"], a["new_string"])
                            with state.lock:
                                state.action_history.append({"type": "write_file", "path": full_path, "backup": backup})
                        elif a["tool"] == "delete_file":
                            r = tool_delete_file(a["path"])
                        elif a["tool"] == "memory_save":
                            r = tool_memory_save(a["key"], a["value"])
                        safe_send(message.chat.id, f"⚡ *Auto:* `{a['tool']}`\n```\n{r[:2000]}\n```")
                    except Exception as e:
                        safe_send(message.chat.id, f"❌ *Auto ошибка:* `{a['tool']}`\n{e}")
            else:
                for a in dangerous_actions:
                    aid = add_pending(a)
                    markup = InlineKeyboardMarkup()
                    markup.add(
                        InlineKeyboardButton("✅ Выполнить", callback_data=f"tool:{aid}:exec"),
                        InlineKeyboardButton("❌ Отмена", callback_data=f"tool:{aid}:cancel")
                    )
                    if a["tool"] == "execute_command":
                        preview_text = f"⚠️ *Запрос на выполнение команды:*\n```bash\n{a['command']}\n```"
                    elif a["tool"] == "write_file":
                        content_preview = a['content'][:500] + "..." if len(a['content']) > 500 else a['content']
                        preview_text = f"📝 *Запрос на запись файла:*\n`{a['path']}`\n\n```\n{content_preview}\n```"
                    elif a["tool"] == "edit_file":
                        preview_text = (f"✏️ *Запрос на редактирование:*\n`{a['path']}`\n\n"
                                        f"*Заменить:*\n```\n{a['old_string'][:300]}\n```\n"
                                        f"*На:*\n```\n{a['new_string'][:300]}\n```")
                    elif a["tool"] == "delete_file":
                        preview_text = f"🗑 *Запрос на удаление:*\n`{a['path']}`"
                    elif a["tool"] == "memory_save":
                        preview_text = f"🧠 *Запрос на сохранение заметки:*\nКлюч: `{a['key']}`\n```\n{a['value'][:500]}\n```"
                    else:
                        preview_text = f"⚠️ *Запрос:* `{a['tool']}`"
                    bot.send_message(message.chat.id, preview_text, reply_markup=markup, parse_mode="Markdown")

        # Summary check
        need_summary = False
        with state.lock:
            if len(state.messages) // 2 >= CONTEXT_LIMIT:
                need_summary = True

        if need_summary:
            stop_typing.clear()
            typing_thread2 = threading.Thread(target=typing_loop, args=(message.chat.id, stop_typing), daemon=True)
            typing_thread2.start()
            try:
                with state.lock:
                    success, result = summarize_context(state)
                if success:
                    bot.send_message(
                        message.chat.id,
                        f"📝 *Контекст суммаризирован* (`{CONTEXT_LIMIT}` пар)\n"
                        f"_Активных пар: `{len(state.messages)//2}`._",
                        parse_mode="Markdown",
                    )
                else:
                    bot.send_message(
                        message.chat.id,
                        f"⚠️ *Суммаризация не удалась*\n`{result[:200]}`",
                        parse_mode="Markdown",
                    )
            finally:
                stop_typing.set()
                typing_thread2.join(timeout=1)

    finally:
        stop_typing.set()
        typing_thread.join(timeout=1)

# ========== GRACEFUL SHUTDOWN ==========
def cleanup():
    logger.info("Bot shutting down gracefully...")
    state = get_state(ADMIN_ID)
    with state.lock:
        for timer in state.reminders:
            try:
                timer.cancel()
            except Exception:
                pass
    save_summary(get_state(ADMIN_ID).summary)
    save_memory(ai_memory)
    save_aliases(command_aliases)
    save_custom_config()

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(cleanup)

# ========== STARTUP ==========
def main():
    logger.info(f"Bot v8 started. Admin={ADMIN_ID}, Provider={DEFAULT_PROVIDER}, Model={DEFAULT_MODEL}, Workspace={WORKSPACE}")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    main()
