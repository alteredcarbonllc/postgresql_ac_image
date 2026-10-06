# PostgreSQL: этап 2 — конфигурационные релизы

Для текущего VPS после успешного этапа 1. PostgreSQL остаётся 16.6,
образ 61ab21d5…; сетевые адреса и рабочие bind mounts сохраняются.
Это ручной деплой конфигурации. Сборка нового образа и Woodpecker CI ещё
не включены. Переход на физический Devuan требует отдельной адаптации:
проверка cgroup сейчас ожидает ac-postgresql-runit.service.

## Установка через Git

Добавить ops/config-deploy в postgresql_ac_image на ноутбуке, проверить:

    python3 -m unittest discover -s ops/config-deploy/tests -v

Сделать commit/push. На VPS выполнить git pull --ff-only, затем:

    sudo python3 ops/config-deploy/install-v2.py
    sudo /usr/local/sbin/ac-pgctl check
    sudo /usr/local/sbin/ac-pg-config check /root/git/postgresql_config

install-v2.py одноразовый, проверяет точные байты установленного runtime v1,
здоровье БД и legacy guards. Сохраняет старый runtime и inspect, создаёт
active.json и атомарно обновляет runtime. БД не останавливает, runsv не трогает.
Если установка прервана и active.json уже создан, НЕ удалять его для повтора:
проверить tool-upgrade.* и фактически установленные файлы.

check читает только закоммиченные regular blobs трёх файлов конфигурации,
отказывается от грязного checkout. Копия релиза по полному commit SHA находится
в /var/lib/ac-postgresql/config-releases. Файлы root-owned 0444, монтируются ro.
Git checkout остаётся источником, а не рабочим mount.

Тестовый кластер создаётся внутри отдельного контейнера с --network none;
рабочий PGDATA никогда не подключается к тесту. Три тестовых переопределения
через -c проверяются по фактическим значениям и исключаются из ошибок
pg_file_settings строго по имени и тексту ошибки. HBA/ident проверяются полностью.
Неудачный тест сохраняется остановленным для диагностики, успешный удаляется.

## Применение после CONFIG_PREFLIGHT_OK

    sudo /usr/local/sbin/ac-pg-config deploy /root/git/postgresql_config
    sudo /usr/local/sbin/ac-pgctl check
    sudo /usr/local/sbin/ac-pgctl status

deploy повторяет изолированный тест ДО остановки рабочей БД. Затем:
1. Сохраняет transaction.json и pending-config-deploy.json под общим lock.
2. Ставит runit down, штатно завершает PostgreSQL SIGINT, проверяет exit 0.
3. Переименовывает остановленный контейнер в имя rollback.
4. Создаёт новый контейнер тем же образом и с теми же данными/сетью/окружением.
   Добавляет RO /etc/postgresql, явный config_file, SIGINT и timeout 120.
   Сохраняет hostname, shm-size 65536000, pids-limit 0, nproc 4194304,
   k8s-file. До старта сверяет ограничения и security options с исходными.
5. Обновляет active.json, запускает новый контейнер через существующий runit,
   проверяет четыре БД, effective config paths, ошибки конфигурации и cgroup.
6. Сохраняет completed и снимает pending. Старый контейнер остаётся остановленным.

Не выполняет podman --replace, rm рабочего/старого контейнера, initdb рабочего
PGDATA, восстановление дампов или смену владельца данных.
ac_containers.service не останавливается и не перезапускается.

## Откат и ограничения

При ошибке после начала переключения сначала ставит runit down и удостоверяется,
что кандидат остановлен; затем переименовывает его в failed, возвращает имя
старого контейнера и active.json, запускает старый через runit и проверяет БД.
Откат восстанавливает контейнер и конфигурацию, не откатывает записи в БД.
Оба контейнера используют один PGDATA: нельзя запускать rollback вручную,
пока работает текущий. Это не резервная копия данных.

SIGINT/TERM/HUP запускают обработчик отката. SIGKILL/потеря питания не перехватываются.
При ROLLBACK_FAILED или оставшемся pending следующий deploy блокируется;
нужна диагностика transaction.json, active.json, inspect и down. Нет автоматического
replay и нет команды принудительного удаления pending. SIGKILL не используется
ни для рабочего, ни для тестового PostgreSQL.

Runtime v2 остаётся привязанным к текущему образу, но ID/Cmd/mounts берёт из
root-owned active.json. Для управления используйте ac-pgctl. Этап 1 migrate
и check-legacy после обновления runtime отключены. Старый install.sh не запускать.

Проверка конфигурации не заменяет вход в почту/XMPP и проверку WordPress.
Пароли, глобальные роли и данные не переносятся в репозиторий конфигурации.
postgresql.auto.conf остаётся в PGDATA; деплой отказывается при ALTER SYSTEM
параметрах, чтобы исключить незаметные переопределения.

Локально проверены unit-тесты с имитацией Podman/runit и реальные временные
Git-репозитории. Полный реальный переход выполняется только на VPS после preflight;
reboot и Devuan этим пакетом не проверены.
