# PostgreSQL image CI: build, test, local import

Это этап подготовки нового образа, без автоматического переключения рабочей БД.
Существующий ac-pg-config остаётся привязан к образу PostgreSQL 16.6.
Не редактируйте IMAGE/active.json вручную для обхода этой проверки.

## Что собирается

Containerfile.ci сохраняет Ubuntu 24.04 и PostgreSQL major 16, фиксирует
postgres UID 101 / GID 103, запрещает автоматическое создание пакетного кластера.
Нет runtime entrypoint с initdb, chown или обработкой рабочего PGDATA.
Процесс postgres запускается напрямую; остановка SIGINT.
Исходный Dockerfile сохранён как описание прежнего образа.

Пакеты берутся из текущего Ubuntu-репозитория: точный minor выясняется в CI.
Это воспроизводимый процесс сборки, но НЕ побитово воспроизводимый образ:
для этого потребуются digest базового образа и snapshot/pinning пакетов.
Список пакетов и версия записаны в /usr/local/share/ac-postgresql внутри образа.
Образ получает полный commit SHA как тег и OCI revision label.
Повторная сборка того же SHA использует существующий rootless образ, проверяя
его заново. Импорт запрещает заменять уже импортированный SHA другим image ID.
Если rootless копия удалена, пересборка может дать другой ID; тогда нужен новый
коммит, а не принудительное перетегирование рабочего/импортированного образа.

## Общая блокировка

Сначала обновить nginx_container/ops/ci-builder: добавлен project postgresql,
образ localhost/postgresql-ac и postgres-specific lock postgresql.lock.
Существующие store.lock/nginx.lock/php.lock не пересоздаются.
Установщик теперь атомарно меняет исполняемый файл. Существующие сборки продолжают
держать прежние дескрипторы lock. Правила хранения прежние: последние 3 образа,
не моложе 7 дней, сохранение используемых/родительских/посторонних тегов.

## Установка на VPS

После сохранения файлов в Git и обновления checkout:

    sudo sh /root/git/nginx_container/ops/ci-builder/install.sh
    sudo sh /root/git/postgresql_ac_image/ops/ci/install-import.sh

Первый установщик не включает cleanup и не запускает его.
Второй создаёт root-owned importer, приватный inbox builder и отдельное sudoers
правило только для ac-postgresql-import. Не меняет контейнеры и не выдаёт
builder доступ к дампам, рабочему PGDATA или управлению PostgreSQL.

Перед обновлением nginx checkout проверить git status: серверная копия ранее
отставала; не выполнять reset --hard или старые общие install.sh.
Активировать postgresql_ac_image в Woodpecker ПОСЛЕ установки инструментов.
Если репозиторий уже активен, новый pipeline до установки завершится ошибкой,
но не переключит рабочую БД. После установки запустить manual pipeline.

## Проверки

    python3 -m unittest discover -s ops/ci/tests -v

Реальный CI-тест запускает отдельный rootless контейнер без сети и host mounts,
проверяет UID/GID, версию >=16.6 и <17, locale C.UTF-8, запись/чтение,
pg_dump/pg_restore, штатный SIGINT и сохранность записи после повторного запуска.
Рабочий PGDATA не подключается. Неудачный тест сохраняется для диагностики.

Импорт использует проверенный механизм PHP: snapshot входного tar, ограничения
размера, проверка единственного тега и revision, пересборка sanitised tar только
из regular manifest/config/layers, неизменяемый тег и root-owned receipt.
Receipt: /var/lib/ac-postgresql/imports/FULL_SHA.json.
CI не запускает rootful кандидат и не вызывает ac-pg-config deploy.

После PG_CANDIDATE_READY: получить точную версию и receipt, затем отдельно
восстановить четыре БД из резервной копии в изолированном кандидате. Только после
этого адаптировать контроллер к выбранному образу и испытать переключение/откат.
Автоматизация postgresql_config и обновления host tools остаётся следующим этапом.
