# Разработка

Нужны Python 3.11+, Node.js 22.12+ (проверено с Node.js 24), pnpm 11.25.0. Готовому Windows EXE эти установки не нужны.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
cd frontend
corepack enable
corepack prepare pnpm@11.25.0 --activate
pnpm install --frozen-lockfile
pnpm run check
pnpm run build
cd ..
.venv\Scripts\python -m unittest discover -s backend/tests -v
.venv\Scripts\python run.py
```

Если Corepack отсутствует, установите pnpm 11.25.0 через официальный пакетный менеджер. Зависимости frontend зафиксированы в `pnpm-lock.yaml`, Python — в requirements-файлах. Разрешён только требуемый скрипт установки esbuild; настройка находится в `pnpm-workspace.yaml`.

## Разработка с обновлением интерфейса

В первом терминале из корня: `.venv\Scripts\python run.py --no-browser`. Во втором: `cd frontend`, затем `pnpm dev`. Откройте напечатанный адрес Vite; `/api` проксируется на порт 8765. Для готовой поставки выполните `pnpm run build`: сервер выдаёт содержимое `frontend/dist`.

API можно запускать отдельно и менять интерфейс независимо. Если меняется API-контракт, обновляйте типы в `frontend/src/types.ts` и соответствующие HTTP-тесты. Если меняется финансовое правило, сначала добавьте проверяемый пример в `backend/tests/` и меняйте ядро, а не рисуйте другие цифры на клиенте.

## Запуск и данные

```powershell
.venv\Scripts\python run.py --port 8765 --data-dir C:\FinGuardData --no-browser
```

По умолчанию Windows хранит данные в `%APPDATA%/FinGuard/web`, Linux — в `~/.local/share/FinGuard/web`. `FINGUARD_DATA_DIR` переопределяет каталог. Данные desktop автоматически не открываются. Путь можно проверить при запуске из своего окружения; пользовательские финансовые данные не должны попадать в Git.

Для чистого теста указывайте отдельный временный каталог. Запрещено использовать живую финансовую базу как фикстуру. Тесты сами создают временные базы и закрывают соединения.

## Windows EXE

Сначала соберите frontend, затем запустите `build-windows.bat`. Используется консольная сборка PyInstaller с включённым frontend и Uvicorn. Окно консоли позволяет видеть запуск и завершать сервер Ctrl+C. Сборочный PATH очищается от DLL других программ.

Результат: `dist/FinGuard-Web.exe`. Проверяйте его отдельно от Python-запуска: старт, `/api/health`, загрузку всех JS/CSS, расчёт каскада, запись и повторное открытие. Браузерный кэш не считается проверкой наличия файлов в EXE.

Справочник API доступен по `/api/docs` и работает без интернета. Это справочник схем, не интерактивная отправка запросов.
