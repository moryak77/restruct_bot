# Деплой на VPS

Бот хранит все данные (баны, варны, тикеты, бинды и т.д.) в локальных JSON-файлах в `data/`.
Это значит, что хостинг с «эфемерным» диском (многие бесплатные PaaS вроде Railway/Render без
подключённого volume) будет **стирать все данные бота при каждом обновлении/перезапуске**.
Поэтому для этого бота проще и надёжнее всего — обычный VPS с постоянным диском: там такой
проблемы просто не существует.

## 0. Какой VPS взять

Бот лёгкий (не требует много CPU/RAM), минимальной конфигурации (1 vCPU, 1 ГБ RAM, Ubuntu 22.04
или 24.04) достаточно с большим запасом. Любой провайдер с Ubuntu/Debian и SSH-доступом подойдёт
— конкретных завязок на провайдера в инструкции ниже нет.

## 1. Первоначальная настройка сервера

Подключитесь по SSH (`ssh root@ваш_ip`) и выполните:

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git ffmpeg
```

`ffmpeg` нужен для `cogs/music.py` (проигрывание музыки) — без него музыка не заработает, а
остальной бот запустится нормально.

Создайте отдельного пользователя (не запускайте бота от root):

```bash
adduser --system --group --home /opt/restruct-bot restruct
```

## 2. Код на сервер

Если репозиторий уже на GitHub:

```bash
su - restruct -s /bin/bash
git clone https://github.com/ВАШ_АККАУНТ/ВАШ_РЕПОЗИТОРИЙ.git /opt/restruct-bot
cd /opt/restruct-bot
```

Если репозитория ещё нет на GitHub — см. `README.md` (раздел про установку) для общей структуры,
а на сервер код можно закинуть и без GitHub, например `scp -r` с локальной машины:

```bash
# выполняется на вашем компьютере, не на сервере
scp -r "C:\Users\rusla\OneDrive\Desktop\restruct bot" restruct@ВАШ_IP:/opt/restruct-bot
```

## 3. Виртуальное окружение и зависимости

```bash
cd /opt/restruct-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 4. Секреты и конфиг — вручную, не через git

`.env` и `config.json` специально не попадают в репозиторий (там токен бота и ID вашего
сервера/каналов/ролей) — их нужно перенести отдельно.

С локальной машины:

```bash
scp .env restruct@ВАШ_IP:/opt/restruct-bot/.env
scp config.json restruct@ВАШ_IP:/opt/restruct-bot/config.json
```

Либо создайте их прямо на сервере по образцу `.env.example` / `config.example.json` (`nano .env`).

Проверьте права доступа, чтобы токен не читали посторонние пользователи сервера:

```bash
chmod 600 /opt/restruct-bot/.env
chown -R restruct:restruct /opt/restruct-bot
```

## 5. Автозапуск и автоперезапуск через systemd

Файл `deploy/restruct-bot.service` уже в репозитории — скопируйте его и поправьте пути/пользователя,
если они отличаются от `/opt/restruct-bot` и `restruct`:

```bash
sudo cp /opt/restruct-bot/deploy/restruct-bot.service /etc/systemd/system/restruct-bot.service
sudo systemctl daemon-reload
sudo systemctl enable restruct-bot
sudo systemctl start restruct-bot
```

`Restart=always` в юните — бот сам поднимется, если упадёт или сервер перезагрузится.

## 6. Проверка и логи

```bash
sudo systemctl status restruct-bot
sudo journalctl -u restruct-bot -f    # живые логи, Ctrl+C для выхода
```

Ищите те же строки, что и при обычном запуске (`Загружен модуль: ...`, `Бот запущен как ...`,
`Слэш-команды синхронизируются...`). Если там `SyncWarning` про описание команды — где-то
описание длиннее 100 символов, бот всё равно запустится, но новые/изменённые команды не
появятся в Discord, пока это не поправить.

## 7. Обновление бота после новых правок в коде

```bash
su - restruct -s /bin/bash
cd /opt/restruct-bot
git pull
.venv/bin/pip install -r requirements.txt   # если requirements.txt менялся
exit
sudo systemctl restart restruct-bot
```

`.env` и `config.json` при `git pull` не трогаются — они не в репозитории, так и останутся
на сервере как есть.
