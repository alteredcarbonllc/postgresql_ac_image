# PostgreSQL: этап 1 — передача существующего контейнера runit

Пакет привязан к текущему VPS, PostgreSQL 16.6, образу 61ab21d5… и контейнеру
21161ce0… (полные значения в ac-pg-runtime.py). Это не универсальный деплой.
Dockerfile, версия БД, данные, сеть, ID контейнера и конфигурация не меняются.
Никаких initdb/create/rm/restore в рабочих командах нет.

Предпосылка: backup /var/backups/ac-postgresql/pre-runit.qhYXU2, успешный
отдельный тест восстановления ролей и четырёх БД в restore-test.bwz7ri.
Дампы относятся ко времени их создания; откат управления не восстанавливает дампы.

## Порядок

На ноутбуке добавьте ops/runtime/ в репозиторий postgresql_ac_image,
выполните `python3 -m unittest discover -s ops/runtime/tests -v`, затем commit/push.
На VPS обновите этот репозиторий, затем:

    sudo sh ops/runtime/install.sh
    sudo /usr/local/sbin/ac-pgctl check-legacy

Установка одноразовая, повторное выполнение отказывается перезаписывать файлы.
Создаёт службу в состоянии down, не запускает её и не меняет контейнер.
Не создаёт sudoers. check-legacy только читает конфигурацию и выполняет SQL SELECT/SHOW.

После успешной проверки и готовности к краткому перерыву БД:

    sudo /usr/local/sbin/ac-pgctl migrate
    sudo /usr/local/sbin/ac-pgctl check
    sudo /usr/local/sbin/ac-pgctl status

Миграция берёт /var/lib/ac-postgresql/lock, останавливает только старый ТАЙМЕР,
сверяет точные байты старых start/stop, сохраняет их и inspect в migration.*.
Устанавливает проверенные guard-версии start/stop и маркер /etc/ac/managed/postgresql.
С маркером старые скрипты пропускают PostgreSQL. Остальные блоки сохранены.
Отключает Podman restart=always, отправляет SIGINT (fast shutdown), ждёт до 120 сек,
проверяет ExitCode=0, запускает существующий контейнер через новый runsv.
Четыре БД проверяются SQL-запросами, проверяется неизменность StartedAt и cgroup conmon.
После успеха возвращает прежнее активное состояние старого таймера.
Старый ac_containers.service НИКОГДА не останавливает/перезапускает.

## Остановка и ограничения

runit/control/t подавляет стандартный SIGTERM к podman attach. Вместо него
посылает SIGINT главному PostgreSQL, ждёт штатного завершения. SIGKILL не посылается.
SIGINT прекращает подключения, откатывает активные транзакции, выполняет штатную остановку.
Старые Config.StopSignal=SIGTERM и StopTimeout=10 у контейнера сохраняются:
для управления используйте ac-pgctl, НЕ обычный podman stop/restart.
При следующем этапе пересоздания контейнера будут заданы SIGINT и новый timeout.
В systemd-обёртке KillMode=process, TimeoutStopSec=infinity: долгий shutdown
требует диагностики, а не убийства БД. Не используйте sv force-stop/force-shutdown.

Команды: start, stop, restart, check, status, reload.
reload проверяет синтаксис pg_file_settings/pg_hba_file_rules, посылает pg_reload_conf,
проверяет доступность. Это не доказательство применения всех настроек: нужны журналы
и pg_settings.pending_restart. Конфигурацию на данном этапе не выносим из PGDATA.
Здоровье БД не заменяет проверку входа в почту/XMPP и работы блога после миграции.

## Откат

При ошибке миграции устанавливает down, завершает PostgreSQL через SIGINT,
останавливает новую обёртку, восстанавливает старые скрипты, снимает маркер,
возвращает restart=always и запускает исходный контейнер. Записывает rolled_back.
При неудаче остановки или отката данные не удаляются, выводится ROLLBACK_FAILED
и путь журнала. Не выполняйте ручной старт второго экземпляра с тем же PGDATA.
Rollback исходного контейнера запускается из процесса миграции и не возвращает
его conmon в cgroup старой службы. Это восстановление работы, не точного cgroup.
SIGKILL/потеря питания не перехватываются; после них разберите record.json,
состояние контейнера, маркера и down прежде чем продолжать. Нет авто-replay журнала.

## Сохранение legacy-исходников

В архиве есть ac_containers_service/start_ac_containers.sh и stop_ac_containers.sh:
это установленные серверные версии с guard PostgreSQL. Их необходимо отдельно
сохранить в этом репозитории после сравнения с актуальной веткой. Не запускайте
старый install.sh: он может включить/перезапустить старое управление.
Миграция ставит те же файлы из ops/runtime/legacy/*.after, проверяя *.before.
Не удаляйте маркер после миграции и не возвращайте старые scripts без guard.

## Devuan

runit run/finish/log/control и runtime start/stop/check не требуют systemd.
install.sh и migrate в этой версии предназначены для текущего Ubuntu VPS.
На физическом Devuan потребуется отдельная активация через runsvdir и новый
паспорт контейнера. Нельзя копировать ID текущего VPS в другой Podman storage.
Перенос данных, конфигурационные релизы, сборка/импорт образов и CI — этап 2.

## Проверки пакета

Локальные unit-тесты: инспекция, отказ при дрейфе, SIGINT, timeout без SIGKILL,
чистый exit, атомарные записи и инъекция ошибки миграции с проверкой отката.
Реальные Podman/runit/systemd и сценарий reboot здесь не выполнялись.
