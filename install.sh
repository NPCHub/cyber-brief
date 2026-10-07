#!/usr/bin/env bash
# Установщик Cyber Brief для чистого Linux-сервера (Debian/Ubuntu; на других
# дистрибутивах он проверит условия и скажет, что поставить руками).
#
#   ./install.sh              установить или обновить (можно запускать повторно)
#   ./install.sh --uninstall  остановить службу и снять автозапуск; записи
#                             и настройки остаются на месте
#
# Скрипт идемпотентен: повторный запуск ничего не затирает — существующие
# значения в .env не спрашиваются, образ пересобирается из кэша, юнит
# пересоздаётся тем же содержимым. Работающая служба при повторном запуске
# НЕ перезапускается: в этот момент может идти запись встречи.
#
# Каждый шаг проверяется явно. Внешние команды в shell не бросают исключений,
# а молчаливый провал на середине хуже явного отказа.
set -Eeuo pipefail

# Скрипт стоит в корне репозитория; этот каталог и есть AI_BRIEF_HOME.
HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME_DIR/.env"
ENV_EXAMPLE="$HOME_DIR/.env.example"
UNIT_TEMPLATE="$HOME_DIR/service/ai-brief-bot.service.in"
UNIT_NAME="ai-brief-bot"
UNIT_FILE="/etc/systemd/system/$UNIT_NAME.service"
LOGROTATE_FILE="/etc/logrotate.d/$UNIT_NAME"
LOG_FILE="/var/log/$UNIT_NAME.log"
IMAGE="ai-brief-recorder:poc"   # то же имя, что RECORDER_IMAGE в service/bot.py

# --------------------------------------------------------------- вывод

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi

step() { printf '\n%s== %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# Без этого set -e обрывает скрипт посреди шага, и человек видит только
# оборванный вывод. Говорим, на какой строке и какой команде остановились.
trap 'rc=$?; printf "\n%s✗ Установка прервана на строке %s (команда: %s, код %s).%s\n   Исправьте причину и запустите ./install.sh ещё раз: он продолжит с того же места.\n" "$R" "$LINENO" "$BASH_COMMAND" "$rc" "$N" >&2' ERR

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE=install
for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    -h|--help) usage; exit 0 ;;
    *) die "Неизвестный параметр: $arg. Справка: ./install.sh --help" ;;
  esac
done

# --------------------------------------------------------------- проверки среды

[[ "$(uname -s)" == "Linux" ]] || die "Нужен Linux (systemd и docker на сервере). Сейчас: $(uname -s)."

if [[ $EUID -ne 0 ]]; then
  # Служба, docker и /etc требуют root. Перезапускаем себя через sudo,
  # а не просим человека помнить об этом.
  command -v sudo >/dev/null 2>&1 || die "Нужен root или sudo. Запустите: su -c ./install.sh"
  info "Нужны права root — перезапускаюсь через sudo."
  exec sudo -E bash "${BASH_SOURCE[0]}" "$@"
fi

command -v systemctl >/dev/null 2>&1 || die "Не найден systemd (systemctl). Установщик рассчитан на сервер с systemd."

# --------------------------------------------------------------- удаление

if [[ "$MODE" == "uninstall" ]]; then
  step "Удаление службы $UNIT_NAME"
  if [[ -f "$UNIT_FILE" ]]; then
    systemctl stop "$UNIT_NAME" 2>/dev/null || warn "Не удалось остановить службу (возможно, уже остановлена)."
    systemctl disable "$UNIT_NAME" 2>/dev/null || warn "Не удалось снять автозапуск."
  else
    info "Служба не была установлена."
  fi
  rm -f "$UNIT_FILE" "$LOGROTATE_FILE"
  systemctl daemon-reload
  if systemctl is-active -q "$UNIT_NAME" 2>/dev/null; then
    die "Служба всё ещё работает. Остановите вручную: systemctl stop $UNIT_NAME"
  fi
  ok "Служба остановлена, автозапуск снят, юнит удалён."
  info "Остались на месте: $ENV_FILE, jobs/ (записи), voices/, profile/, лог $LOG_FILE"
  info "Образ записи не трогал. Убрать его: docker rmi $IMAGE"
  info "Установить снова: ./install.sh"
  exit 0
fi

# --------------------------------------------------------------- 1. зависимости

step "1/5. Проверка зависимостей"

[[ -f "$UNIT_TEMPLATE" && -f "$ENV_EXAMPLE" && -f "$HOME_DIR/poc/recorder/Dockerfile" ]] \
  || die "Запускайте из корня склонированного репозитория: не найдены service/, .env.example или poc/recorder/."

PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG=apt; fi

need_pkgs=()
apt_install() {
  [[ "$PKG" == "apt" ]] || die "Не найден менеджер пакетов apt. Поставьте вручную: $*"
  info "Ставлю: $*"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update не прошёл — проверьте интернет и репозитории."
  apt-get install -y -qq "$@" || die "Не удалось поставить пакеты: $*"
}

# curl нужен самому установщику (проверка токенов) и боту (загрузка в gpu-hub).
for tool in curl tar; do
  command -v "$tool" >/dev/null 2>&1 || need_pkgs+=("$tool")
done
command -v ffmpeg >/dev/null 2>&1 || need_pkgs+=(ffmpeg)

py_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; }
py_ok || need_pkgs+=(python3)

if ! command -v docker >/dev/null 2>&1; then
  need_pkgs+=(docker.io)
fi

if [[ ${#need_pkgs[@]} -gt 0 ]]; then
  apt_install "${need_pkgs[@]}"
fi

py_ok || die "Нужен Python 3.11 или новее, а стоит: $(python3 --version 2>&1 || echo 'не найден'). Поставьте новее (на Ubuntu 22.04 — через deadsnakes или обновите систему до 24.04) и запустите установщик снова."
PYTHON="$(command -v python3)"
ok "python3: $($PYTHON --version 2>&1)"

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg не установился. Поставьте вручную и запустите снова."
ok "ffmpeg: найден"
command -v curl >/dev/null 2>&1 || die "curl не установился. Поставьте вручную и запустите снова."

command -v docker >/dev/null 2>&1 || die "docker не установился. Поставьте по инструкции https://docs.docker.com/engine/install/ и запустите снова."
systemctl enable --now docker >/dev/null 2>&1 || warn "Не удалось включить службу docker через systemctl — проверяю, отвечает ли он."
docker info >/dev/null 2>&1 || die "docker установлен, но не отвечает. Посмотрите: systemctl status docker"
ok "docker: $(docker --version)"

# --------------------------------------------------------------- 2. настройки

step "2/5. Настройки (.env)"

FRESH=0
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  FRESH=1
  ok "Создан $ENV_FILE из примера."
else
  ok "Найден $ENV_FILE — существующие значения не трогаю."
fi
chmod 600 "$ENV_FILE"

# Значение ключа из .env: последнее непустое определение. Без source: файл —
# не shell-скрипт (в значениях бывают пробелы и «=>»).
get_env() {
  "$PYTHON" - "$ENV_FILE" "$1" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
val = ""
for line in open(path, encoding="utf-8").read().splitlines():
    if line.lstrip().startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == key and v.strip():
        val = v.strip()
print(val)
PY
}

# Запись значения без попадания секрета в аргументы процесса (их видно в ps):
# значение едет через переменную окружения.
set_env() {
  KEY="$1" VAL="$2" "$PYTHON" - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1])
key, val = os.environ["KEY"], os.environ["VAL"]
lines = p.read_text(encoding="utf-8").splitlines()
out, done = [], False
for line in lines:
    if not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == key:
        if not done:
            out.append(f"{key}={val}")
            done = True
        continue  # дубликаты ключа убираем: читается всё равно первый
    out.append(line)
if not done:
    out.append(f"{key}={val}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  chmod 600 "$ENV_FILE"
}

INTERACTIVE=0
[[ -t 0 ]] && INTERACTIVE=1

# Секреты curl получает конфигом со stdin, а не в аргументах: иначе токен
# виден в списке процессов любому пользователю сервера.
tg_get_me() {  # $1 = токен; печатает JSON
  printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" | curl -sS -m 20 -K - 2>/dev/null
}
json_field() {  # $1 = JSON, $2 = выражение над d
  printf '%s' "$1" | "$PYTHON" -c "import json,sys
try:
    d=json.load(sys.stdin)
    print($2)
except Exception:
    print('')"
}

ask_token() {
  local tok="" attempt resp name
  tok="$(get_env TELEGRAM_BOT_TOKEN)"
  [[ -z "$tok" && -n "${TELEGRAM_BOT_TOKEN:-}" ]] && tok="$TELEGRAM_BOT_TOKEN"
  if [[ -z "$tok" ]]; then
    [[ $INTERACTIVE -eq 1 ]] || die "TELEGRAM_BOT_TOKEN не задан, а запуск не интерактивный. Впишите токен в $ENV_FILE и запустите снова."
    echo "  Токен бота: создайте бота у @BotFather в Telegram (/newbot) и скопируйте токен."
  fi
  for attempt in 1 2 3; do
    if [[ -z "$tok" ]]; then
      read -rsp "  Токен бота (ввод скрыт): " tok; echo
    fi
    if [[ ! "$tok" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
      warn "Не похоже на токен (ожидается 123456:AA...)."
      tok=""; [[ $INTERACTIVE -eq 1 ]] || die "Неверный формат TELEGRAM_BOT_TOKEN в $ENV_FILE."
      continue
    fi
    if resp="$(tg_get_me "$tok")" && [[ -n "$resp" ]]; then
      name="$(json_field "$resp" "d['result']['username'] if d.get('ok') else ''")"
      if [[ -n "$name" ]]; then
        set_env TELEGRAM_BOT_TOKEN "$tok"
        BOT_USERNAME="$name"; BOT_TOKEN="$tok"
        ok "Токен принят: @$name"
        return 0
      fi
      warn "Telegram не принял этот токен. Проверьте, что скопировали целиком."
      tok=""; [[ $INTERACTIVE -eq 1 ]] || die "Telegram отклонил TELEGRAM_BOT_TOKEN из $ENV_FILE."
    else
      warn "Не удалось связаться с api.telegram.org (сеть или блокировка)."
      warn "Сохраняю токен без проверки — служба не заработает, пока сервер не достучится до Telegram."
      set_env TELEGRAM_BOT_TOKEN "$tok"; BOT_TOKEN="$tok"; BOT_USERNAME=""
      return 0
    fi
  done
  die "Три неудачные попытки ввести токен. Запустите ./install.sh ещё раз."
}

ask_openai() {  # $1 = required | optional
  local need="$1" key="" attempt code
  key="$(get_env OPENAI_API_KEY)"
  [[ -z "$key" && -n "${OPENAI_API_KEY:-}" ]] && key="$OPENAI_API_KEY"
  if [[ -z "$key" ]]; then
    if [[ "$need" == optional ]]; then
      [[ $INTERACTIVE -eq 1 ]] || { warn "OPENAI_API_KEY не задан: резервной расшифровки нет."; return 0; }
      echo "  Ключ OpenAI нужен только как резерв: расшифровать встречу, если машина"
      echo "  с видеокартой не ответила. Без него такая встреча будет ждать машину."
    else
      [[ $INTERACTIVE -eq 1 ]] || die "OPENAI_API_KEY не задан, а запуск не интерактивный. Впишите ключ в $ENV_FILE и запустите снова."
      echo "  Ключ OpenAI: https://platform.openai.com/api-keys (на счёте должны быть деньги)."
    fi
  fi
  for attempt in 1 2 3; do
    if [[ -z "$key" ]]; then
      if [[ "$need" == optional ]]; then
        read -rsp "  Ключ OpenAI (ввод скрыт, Enter — без резерва): " key; echo
        if [[ -z "$key" ]]; then warn "Без резерва: встречи ждут машину с видеокартой."; return 0; fi
      else
        read -rsp "  Ключ OpenAI (ввод скрыт): " key; echo
      fi
    fi
    if [[ ! "$key" =~ ^[A-Za-z0-9_-]{20,}$ ]]; then
      warn "Не похоже на ключ OpenAI (ожидается sk-...)."
      key=""; [[ $INTERACTIVE -eq 1 ]] || die "Неверный формат OPENAI_API_KEY в $ENV_FILE."
      continue
    fi
    code="$(printf 'url = "https://api.openai.com/v1/models"\nheader = "Authorization: Bearer %s"\n' "$key" \
            | curl -s -m 20 -o /dev/null -w '%{http_code}' -K - 2>/dev/null || true)"
    case "$code" in
      200) set_env OPENAI_API_KEY "$key"; ok "Ключ OpenAI принят."; return 0 ;;
      401) warn "OpenAI отклонил ключ (401). Проверьте, что скопировали целиком и ключ не отозван."
           key=""; [[ $INTERACTIVE -eq 1 ]] || die "OpenAI отклонил OPENAI_API_KEY из $ENV_FILE." ;;
      *)   warn "OpenAI ответил кодом '${code:-нет ответа}' — проверить ключ не удалось (сеть, блокировка региона или сбой)."
           warn "Сохраняю ключ без проверки: если он неверный, бот скажет об этом при первой встрече."
           set_env OPENAI_API_KEY "$key"; return 0 ;;
    esac
  done
  die "Три неудачные попытки ввести ключ. Запустите ./install.sh ещё раз."
}

# Машина с видеокартой — основной путь расшифровки: точнее на русском,
# узнаёт голоса по банку образцов, бесплатна. OpenAI остаётся резервом
# на случай, если машина не ответила.
hub_status() {  # $1 = адрес, $2 = ключ; печатает «код тело»
  printf 'url = "%s/v1/status"\nheader = "Authorization: Bearer %s"\n' "$1" "$2" \
    | curl -s -m 20 -w '\n%{http_code}' -K - 2>/dev/null || true
}

ask_hub() {
  local url key ans attempt resp code body online
  url="$(get_env GPUHUB_URL)"; key="$(get_env GPUHUB_KEY)"
  if [[ -z "$url" || -z "$key" ]]; then
    [[ $INTERACTIVE -eq 1 ]] || { warn "gpu-hub не задан: расшифровка пойдёт через OpenAI."; return 0; }
    echo "  Расшифровка на своей видеокарте через gpu-hub — основной путь, OpenAI тогда"
    echo "  только резерв. Адрес и ключ выдаёт владелец хаба."
    read -rp "  Подключить gpu-hub? [Y/n] " ans
    if [[ ! "${ans:-Y}" =~ ^[YyДд] ]]; then
      set_env STT_BACKEND openai
      ok "Без gpu-hub: расшифровка через OpenAI. Подключить позже — ./install.sh ещё раз."
      return 0
    fi
  fi
  for attempt in 1 2 3; do
    if [[ -z "$url" ]]; then read -rp "  Адрес хаба (https://...): " url; fi
    url="${url%/}"
    if [[ ! "$url" =~ ^https?://[^[:space:]]+$ ]]; then
      warn "Ожидается адрес вида https://hub.example.com"; url=""; continue
    fi
    if [[ -z "$key" ]]; then read -rsp "  Ключ хаба (ввод скрыт): " key; echo; fi
    resp="$(hub_status "$url" "$key")"
    code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
    case "$code" in
      200)
        online="$(json_field "$body" "sum(1 for w in d.get('workers',[]) if w.get('online') and 'transcribe.meeting' in (w.get('skills') or []))")"
        set_env GPUHUB_URL "$url"; set_env GPUHUB_KEY "$key"
        set_env STT_BACKEND local-first
        # Своя карта важнее денег за OpenAI: ждём её дольше, чем пару минут.
        [[ -n "$(get_env LOCAL_WAIT_MIN)" && $FRESH -eq 0 ]] || set_env LOCAL_WAIT_MIN 120
        ok "gpu-hub подключён: $url"
        if [[ "${online:-0}" -gt 0 ]]; then
          ok "Машин с расшифровкой на связи: $online"
        else
          warn "Сейчас ни одна машина с расшифровкой не на связи. Это не ошибка установки:"
          warn "встречи будут ждать её $(get_env LOCAL_WAIT_MIN) мин, потом уйдут в OpenAI."
        fi
        return 0 ;;
      401|403)
        warn "Хаб отклонил ключ ($code). Проверьте, что ключ клиентский и не отозван."
        key=""; [[ $INTERACTIVE -eq 1 ]] || die "gpu-hub отклонил GPUHUB_KEY из $ENV_FILE." ;;
      *)
        warn "Хаб не ответил по адресу $url (код '${code:-нет ответа}')."
        url=""; key=""
        [[ $INTERACTIVE -eq 1 ]] || die "gpu-hub недоступен по GPUHUB_URL из $ENV_FILE." ;;
    esac
  done
  warn "Три неудачные попытки. Продолжаю без gpu-hub: расшифровка через OpenAI."
  set_env STT_BACKEND openai
}

# Бриф собирает языковая модель. Подходит любой сервис с OpenAI-совместимым
# API: свой адрес, ключ и модель. По умолчанию — OpenAI.
llm_probe() {  # $1 = адрес, $2 = ключ, $3 = модель; печатает «тело\nкод»
  printf 'url = "%s/chat/completions"\nheader = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\ndata = "{\\"model\\":\\"%s\\",\\"max_tokens\\":1,\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"1\\"}]}"\n' \
    "$1" "$2" "$3" | curl -s -m 30 -w '\n%{http_code}' -K - 2>/dev/null || true
}

LLM_IS_OPENAI=1
ask_llm() {
  local url key model ans attempt resp code
  url="$(get_env LLM_BASE_URL)"
  if [[ -n "$url" ]]; then
    LLM_IS_OPENAI=0
    ok "Модель брифа: $(get_env LLM_MODEL) на $url"
    return 0
  fi
  [[ $INTERACTIVE -eq 1 ]] || return 0
  echo "  Модель для брифа: OpenAI или другой сервис с OpenAI-совместимым API"
  echo "  (свой сервер, агрегатор, DeepSeek и т. п.)."
  read -rp "  Использовать другой сервис? [y/N] " ans
  if [[ ! "${ans:-N}" =~ ^[YyДд] ]]; then ok "Бриф собирает OpenAI."; return 0; fi
  for attempt in 1 2 3; do
    [[ -n "${url:-}" ]] || read -rp "  Адрес API (https://.../v1): " url
    url="${url%/}"; url="${url%/chat/completions}"
    if [[ ! "$url" =~ ^https?://[^[:space:]]+$ ]]; then warn "Ожидается адрес вида https://api.example.com/v1"; url=""; continue; fi
    [[ -n "${key:-}" ]] || { read -rsp "  Ключ (ввод скрыт): " key; echo; }
    [[ -n "${model:-}" ]] || read -rp "  Модель (как её называет сервис): " model
    if [[ -z "$model" ]]; then warn "Нужно имя модели."; continue; fi
    resp="$(llm_probe "$url" "$key" "$model")"
    code="${resp##*$'\n'}"
    case "$code" in
      200)
        set_env LLM_BASE_URL "$url"; set_env LLM_API_KEY "$key"; set_env LLM_MODEL "$model"
        LLM_IS_OPENAI=0
        ok "Модель брифа: $model на $url — пробный запрос прошёл."
        return 0 ;;
      401|403) warn "Сервис отклонил ключ ($code)."; key="" ;;
      400|404|422) warn "Сервис не принял модель «$model» (код $code). Проверьте точное имя."; model="" ;;
      *)
        warn "Сервис не ответил (код '${code:-нет ответа}'). Проверьте адрес."; url="" ;;
    esac
  done
  warn "Три неудачные попытки. Бриф будет собирать OpenAI."
}

ask_chats() {
  local cur ans ids
  cur="$(get_env ALLOWED_CHATS)"
  if [[ -n "$cur" ]]; then ok "ALLOWED_CHATS уже задан: $cur"; return 0; fi
  [[ $INTERACTIVE -eq 1 ]] || { warn "ALLOWED_CHATS пуст: бот никого не обслуживает, пока вы не впишете chat_id в $ENV_FILE."; return 0; }
  echo "  Чаты, которым можно пользоваться ботом (числовые chat_id через запятую)."
  echo "  Не знаете id? Оставьте пустым и нажмите Enter — найду по вашему сообщению боту."
  read -rp "  chat_id: " ans
  ans="${ans// /}"
  if [[ -z "$ans" && -n "${BOT_TOKEN:-}" ]]; then
    echo "  Откройте бота ${BOT_USERNAME:+@$BOT_USERNAME }в Telegram, нажмите Start или напишите ему любое сообщение,"
    read -rp "  затем нажмите Enter здесь... " _
    ids="$(printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$BOT_TOKEN" | curl -sS -m 20 -K - 2>/dev/null \
      | "$PYTHON" -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    d={}
seen={}
for u in d.get('result',[]):
    m=u.get('message') or u.get('channel_post') or {}
    c=m.get('chat') or {}
    if c.get('type')=='private' and c.get('id'):
        seen[c['id']]=(c.get('first_name') or c.get('username') or '')
print(','.join(f'{k}:{v}' for k,v in seen.items()))" || true)"
    if [[ -n "$ids" ]]; then
      # Берём первый личный чат; остальные покажем, но не добавим молча.
      local first="${ids%%,*}"
      read -rp "  Нашёл чат ${first#*:} (id ${first%%:*}). Разрешить его? [Y/n] " yn
      if [[ "${yn:-Y}" =~ ^[YyДд] ]]; then ans="${first%%:*}"; fi
    else
      warn "Сообщений боту не нашёл. Впишите chat_id позже в $ENV_FILE (ALLOWED_CHATS=...)."
    fi
  fi
  if [[ -n "$ans" ]]; then
    if [[ "$ans" =~ ^-?[0-9]+(,-?[0-9]+)*$ ]]; then
      set_env ALLOWED_CHATS "$ans"; ok "ALLOWED_CHATS записан."
    else
      warn "Ожидаются только числа через запятую — не записываю. Исправьте позже в $ENV_FILE."
    fi
  else
    warn "ALLOWED_CHATS пуст: бот ответит каждому только его chat_id. Впишите нужный в $ENV_FILE и перезапустите службу."
  fi
}

ask_tz() {
  local cur ans def
  cur="$(get_env TZ)"
  # Спрашиваем только при первой установке: потом это уже осознанный выбор.
  if [[ -n "$cur" && $FRESH -eq 0 ]]; then ok "TZ уже задан: $cur"; return 0; fi
  def="${cur:-$(timedatectl show -p Timezone --value 2>/dev/null || true)}"
  def="${def:-Europe/Moscow}"
  ans="$def"
  if [[ $INTERACTIVE -eq 1 ]]; then
    read -rp "  Часовой пояс [$def]: " ans
    ans="${ans:-$def}"
  fi
  if [[ ! -e "/usr/share/zoneinfo/$ans" ]]; then
    warn "Нет такого пояса: $ans. Беру $def (список: timedatectl list-timezones)."
    ans="$def"
  fi
  set_env TZ "$ans"
  ok "TZ=$ans"
}

BOT_USERNAME=""; BOT_TOKEN=""
ask_token
ask_hub
ask_llm
# OpenAI обязателен, только если без него нечем расшифровать или собрать
# бриф. С gpu-hub и своей моделью брифа он лишь резерв расшифровки.
if [[ "$(get_env STT_BACKEND)" == local-first && $LLM_IS_OPENAI -eq 0 ]]; then
  ask_openai optional
else
  ask_openai required
fi
ask_chats
ask_tz

mkdir -p "$HOME_DIR/jobs" "$HOME_DIR/voices" "$HOME_DIR/profile"
ok "Каталоги данных готовы: jobs/ voices/ profile/"

# --------------------------------------------------------------- 3. образ записи

step "3/5. Образ записи ($IMAGE)"
info "Первая сборка занимает 5–15 минут и ~2 ГБ на диске; повторная — секунды (кэш)."
# На слабом сервере (2 ядра) сборка заметно грузит процессор: если на нём
# живут другие сайты, запускайте установку в тихое время.
if ! docker build -t "$IMAGE" "$HOME_DIR/poc/recorder"; then
  die "Сборка образа не удалась. Частые причины: нет места на диске (df -h /), нет доступа к deb.debian.org или pypi.org. Исправьте и запустите ./install.sh снова — кэш сохранён."
fi
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "docker build завершился, но образа $IMAGE нет. Запустите ./install.sh снова."
ok "Образ собран."

# --------------------------------------------------------------- 4. служба

step "4/5. Служба systemd с автозапуском"

TZ_VALUE="$(get_env TZ)"; TZ_VALUE="${TZ_VALUE:-Europe/Moscow}"
UNIT_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP"' EXIT
# Подстановка через python, а не sed: путь установки может содержать «&» и «|».
"$PYTHON" - "$UNIT_TEMPLATE" "$UNIT_TMP" "$HOME_DIR" "$TZ_VALUE" "$PYTHON" <<'PY'
import sys, pathlib
tpl, out, home, tz, py = sys.argv[1:6]
text = pathlib.Path(tpl).read_text(encoding="utf-8")
for key, val in (("@AI_BRIEF_HOME@", home), ("@TZ@", tz), ("@PYTHON@", py)):
    text = text.replace(key, val)
if "@" in "".join(l for l in text.splitlines() if not l.lstrip().startswith("#")):
    sys.exit("в юните остались неподставленные значения")
pathlib.Path(out).write_text(text, encoding="utf-8")
PY
install -m 644 "$UNIT_TMP" "$UNIT_FILE"
touch "$LOG_FILE"
chmod 640 "$LOG_FILE"

# Лог дописывается бесконечно: без ротации через пару месяцев он съест диск.
cat > "$LOGROTATE_FILE" <<EOF
$LOG_FILE {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF

systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || die "Не удалось включить автозапуск: systemctl enable $UNIT_NAME"
systemctl is-enabled -q "$UNIT_NAME" || die "Автозапуск не включился (systemctl is-enabled $UNIT_NAME)."
ok "Юнит $UNIT_FILE создан, автозапуск включён."

# --------------------------------------------------------------- 5. запуск

step "5/5. Запуск и проверка"

STARTED=0
if systemctl is-active -q "$UNIT_NAME"; then
  warn "Служба уже работает — не перезапускаю: может идти запись встречи."
  info "Чтобы применить новые настройки: systemctl restart $UNIT_NAME"
else
  systemctl start "$UNIT_NAME" || die "Служба не запустилась. Смотрите: journalctl -u $UNIT_NAME -n 30 и tail $LOG_FILE"
  STARTED=1
fi

# Даём время упасть, если упадёт: Restart=always скроет падение за «active»
# на первых секундах.
alive=0
for _ in 1 2 3 4 5 6 7 8; do
  sleep 2
  if systemctl is-active -q "$UNIT_NAME"; then alive=1; else alive=0; break; fi
done
RESTARTS="$(systemctl show "$UNIT_NAME" -p NRestarts --value 2>/dev/null || echo 0)"

# Счётчик перезапусков осмыслен только для службы, которую мы сами только что
# подняли: у давно работающей он хранит историю и не говорит о её здоровье.
if [[ $alive -eq 1 && ( $STARTED -eq 0 || "${RESTARTS:-0}" == "0" ) ]]; then
  ok "Служба работает и включена в автозапуск."
else
  warn "Служба не держится на ногах (перезапусков: ${RESTARTS:-?}). Последние строки лога:"
  tail -n 10 "$LOG_FILE" >&2 || true
  die "Исправьте причину из лога выше и запустите ./install.sh снова."
fi

if [[ -n "$BOT_TOKEN" ]]; then
  if resp="$(tg_get_me "$BOT_TOKEN")" && [[ -n "$(json_field "$resp" "d['result']['username'] if d.get('ok') else ''")" ]]; then
    ok "Бот отвечает Telegram: @$BOT_USERNAME"
  else
    warn "Бот не отвечает Telegram: проверьте, что сервер достаёт api.telegram.org."
  fi
fi
if tail -n 30 "$LOG_FILE" 2>/dev/null | grep -qE 'ВНИМАНИЕ|Telegram отклонил'; then
  warn "В логе есть предупреждения: tail -n 20 $LOG_FILE"
fi

cat <<EOF

${B}Готово.${N} Что дальше:
  1. Откройте бота ${BOT_USERNAME:+@$BOT_USERNAME }в Telegram и отправьте /start.
  2. Пришлите ссылку на встречу в Яндекс Телемосте: https://telemost.yandex.ru/j/...
     Бот зайдёт, запишет и пришлёт бриф в этот чат. Состояние — команда /status.
  3. Если бот отвечает «чат не разрешён» — впишите показанный им chat_id в
     $ENV_FILE (ALLOWED_CHATS=...) и выполните: systemctl restart $UNIT_NAME

Полезное:
  лог            tail -f $LOG_FILE
  статус         systemctl status $UNIT_NAME
  перезапуск     systemctl restart $UNIT_NAME   (не во время записи)
  удалить службу ./install.sh --uninstall       (записи и настройки останутся)
EOF
