#!/usr/bin/env bash
# Смена токена Telegram-бота без передачи секрета в переписку.
# Токен вводится вручную и не печатается на экран, в историю shell не попадает.
# Запускать на том сервере, где стоит AI-Brief:
#
#   sudo ./service/set-token.sh
#
# С удалённой машины: ssh -t <ваш-сервер> sudo /путь/к/ai-brief/service/set-token.sh
set -euo pipefail

# Каталог установки — на уровень выше service/ (или AI_BRIEF_HOME, если задан).
HOME_DIR="${AI_BRIEF_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV="$HOME_DIR/.env"
UNIT=ai-brief-bot
LOG="/var/log/$UNIT.log"

[[ -f "$ENV" ]] || { echo "Не найден $ENV. Сначала выполните ./install.sh" >&2; exit 1; }
[[ -w "$ENV" ]] || { echo "Нет прав на запись в $ENV — запустите через sudo." >&2; exit 1; }

read -rsp "Токен бота (ввод скрыт): " TOKEN
echo

if [[ ! "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "Не похоже на токен бота (ожидается 123456:AA...). Ничего не менял." >&2
  exit 1
fi

# Сначала проверяем, что токен живой, и только потом трогаем конфиг.
# Токен отдаём curl через конфиг со stdin: в аргументах его видно в ps.
NAME=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TOKEN" \
  | curl -s -m 20 -K - \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' \
  2>/dev/null || true)

if [[ -z "$NAME" ]]; then
  echo "Telegram не принял этот токен (или нет связи с api.telegram.org). Ничего не менял." >&2
  exit 1
fi

cp "$ENV" "$ENV.bak.$(date +%Y%m%d-%H%M%S)"
# Пишем через переменную окружения, чтобы токен не оказался в аргументах
# процесса, где его видно в ps.
TOKEN="$TOKEN" python3 - "$ENV" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding="utf-8").splitlines()
new = f"TELEGRAM_BOT_TOKEN={os.environ['TOKEN']}"
out, done = [], False
for l in lines:
    if l.startswith("TELEGRAM_BOT_TOKEN="):
        if not done:
            out.append(new)
            done = True
        continue
    out.append(l)
if not done:
    out.append(new)
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chmod 600 "$ENV"
unset TOKEN

echo "Токен принят: @${NAME}"

# Новый бот — новая переписка, старый chat_id к нему не относится.
python3 - "$ENV" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
out = ["ALLOWED_CHATS=" if l.startswith("ALLOWED_CHATS=") else l
       for l in p.read_text(encoding="utf-8").splitlines()]
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
echo "ALLOWED_CHATS очищен: напишите новому боту, он подскажет свой chat_id."
echo "Копия прежних настроек: $ENV.bak.* (в ней старый токен — удалите, когда убедитесь, что всё работает)."

systemctl restart "$UNIT"
sleep 3
systemctl is-active "$UNIT"
tail -3 "$LOG"
