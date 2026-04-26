# VPN-prototype (HTTP Proxy A -> Socket.IO -> Proxy B)

Проект состоит из 2 серверов:

- `local_proxy_server.py` - локальный HTTP-прокси (`127.0.0.1:6767`), включает системный прокси в Windows и пересылает трафик через Socket.IO.
- `remote_proxy_server.py` - удаленный сервер, принимает сообщения от локального прокси, делает реальный HTTP/HTTPS запрос в интернет и возвращает ответ.

## Что реализовано

- Поддержка обычных HTTP методов (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`).
- Поддержка `CONNECT` для HTTPS (туннель через custom события Socket.IO).
- Включение системного прокси при старте локального сервера и отключение при нормальном завершении/краше процесса (через `atexit`).
- Свой JSON-протокол поверх Socket.IO (см. `shared_protocol.py`).
- Ротация удаленного узла каждый час (или другой интервал) через `--rotate-every-seconds`.

## Установка

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Для удаленного сервера (Render/VPS) дополнительно:

```powershell
pip install -r requirements-remote.txt
```

## Запуск

### 1) Запуск удаленного прокси (сервер B)

На удаленной машине:

```powershell
python remote_proxy_server.py --host 0.0.0.0 --port 9000
```

Откройте порт `9000` в firewall/security group.

### 2) Запуск локального прокси (сервер A)

На вашей Windows-машине:

```powershell
python local_proxy_server.py --local-port 6767 --remote-urls http://<REMOTE_IP_1>:9000,http://<REMOTE_IP_2>:9000 --rotate-every-seconds 3600
```

После запуска:

- системный proxy включится на `127.0.0.1:6767`;
- приложения, использующие системные proxy-настройки, начнут ходить через цепочку A->B.
- активный удаленный узел будет автоматически переключаться по кругу раз в `3600` секунд.

При остановке (`Ctrl+C`) настройки прокси отключаются автоматически.

## Быстрая проверка

Пока локальный сервер запущен:

```powershell
curl http://example.com
curl https://example.com -k
```

Если ответ приходит, цепочка работает.

## Куда деплоить удаленный сервер

Подойдут любые VPS/VM:

- Hetzner Cloud
- DigitalOcean Droplet
- Vultr
- AWS EC2 / Lightsail
- Oracle Cloud Free Tier

Минимально достаточно 1 vCPU / 1 GB RAM.

## Важно

- В этой демо-версии нет шифрования канала A<->B (как вы и разрешили). Для продакшна добавьте TLS (`https`) между прокси.
- Нет авторизации клиента - любой, кто знает адрес, может подключиться. Для продакшна добавьте токен на этапе Socket.IO connect.
