# Автоматический деплой PostgreSQL configuration

Пакет для текущего VPS: PostgreSQL 16 под runit, rootless Woodpecker local
backend от ac-ci-builder (UID 1001). Конфигурация хранится в отдельном
репозитории alteredcarbonllc/postgresql_config.

## Что устанавливается

- /usr/local/sbin/ac-pg-config-ci — отдельный интерфейс CI.
- /usr/local/libexec/ac-pg-config-ci — root-owned Python и эталоны SHA256.
- /var/spool/ac-pg-config — root:root 0755.
- inbox внутри него — ac-ci-builder 0700; queue.lock — ac-ci-builder 0600.
- /var/lib/ac-postgresql/config-ci-receipts — root-only квитанции.
- /etc/sudoers.d/ac-pg-config-ci — только prepare/apply с проверкой аргументов.

Установщик не изменяет существующие config-deploy.py, ac-pg-runtime.py,
systemd/runit units, legacy guards, active.json или контейнер.
Их совместимость проверяется по двум контрольным суммам установленного Python.
Изменение эталонов требует анализа новой версии, а не автоматического пересчёта.

Режимы каталогов выставляются явно после проверки владельца, типа и запрета
записи для группы/остальных. umask 077 не закрывает доступ CI к inbox.
visudo вызывается по абсолютному пути /usr/sbin/visudo.
Повторный запуск установщика сохраняет флаг enable и сверяет установленные
файлы. При другой версии уже установленного CI-инструмента остановится:
обновление инструмента требует отдельного контролируемого изменения.

## Установка

Сначала добавить ops/config-ci в postgresql_ac_image, выполнить:

    python3 -m unittest discover -s ops/config-ci/tests -v

Закоммитить и отправить main. На VPS:

    cd /root/git/postgresql_ac_image
    git pull --ff-only
    sudo python3 ops/config-ci/install.py
    sudo /usr/local/sbin/ac-pg-config-ci status
    sudo /usr/local/sbin/ac-pgctl check

Первичная установка регистрирует контрольные суммы текущего подключённого
релиза после проверки его пути, владельца и работающего PostgreSQL.
Повторная установка не принимает изменённое содержимое за новый эталон.

Проверить доступ к spool:

    sudo -u ac-ci-builder -- flock -n /var/spool/ac-pg-config/queue.lock /usr/bin/true
    sudo -u ac-ci-builder -- test -w /var/spool/ac-pg-config/inbox

Разрешить применение:

    sudo /usr/local/sbin/ac-pg-config-ci enable

Затем добавить .woodpecker.yaml и ops/ci из части postgresql_config архива
в этот репозиторий, проверить shell/Python синтаксис, commit/push main.
Активировать репозиторий в Woodpecker. Если push был до активации, запустить
manual pipeline для main.

## Pipeline

Pipeline допускает push/manual на main и backend local.
deploy.sh сверяет CI_REPO, CI_COMMIT_BRANCH и CI_PIPELINE_EVENT.
Экспортёр сверяет HEAD с полным CI_COMMIT_SHA и чистоту checkout.
git archive экспортирует только postgresql/, не выполняя код из него от root.
Корневой импортёр принимает ровно:
postgresql/postgresql.conf, postgresql/pg_hba.conf, postgresql/pg_ident.conf.

Запрещены дополнительные файлы, ссылки, устройства, FIFO, исполняемые файлы,
необычные пути, повторы, NUL и не-UTF8. Архив до 4 MiB; каждый файл менее 1 MiB.
Данные читаются в память из ограниченного обычного файла с O_NOFOLLOW,
проверкой владельца и стабильности stat. Извлечение tar на файловую систему
не используется. Новый релиз создаётся в root-owned временном каталоге
и публикуется rename. Файлы 0444, каталог 0755 под закрытым state.
Секретов этот пакет не добавляет; пароли не следует помещать в Git.

CI считается доверенным deploy-пользователем, как в уже работающем mail CI.
Заявленный SHA связан с содержимым квитанцией после первого импорта, но
это не криптографическое подтверждение происхождения коммита от GitHub.
Права на main и local runner остаются частью модели доверия.
Конфигурация PostgreSQL — привилегированная конфигурация сервера, не sandbox
для недоверенных авторов.

## Проверка и применение

Используется существующая /var/lib/ac-postgresql/lock: блокирует параллельные
операции PostgreSQL и пересекающиеся операции mail, которые используют тот же
lock. queue.lock последовательно запускает именно configuration pipelines;
при конкуренции с другим административным действием root lock откажет —
после завершения того действия pipeline можно повторить.

Перед импортом и перед применением проверяется активный контейнер, изображение,
legacy guards, здоровье и совпадение файлов с зарегистрированными хешами.
Незавершённая pending-config-deploy.json блокирует новый запуск.

Изолированный test_release из установленного config-deploy.py запускается
на текущем одобренном образе, с network=none, временной БД и без production
PGDATA. Используются те же тестовые исключения для data_directory,
listen_addresses и unix_socket_directories, которые уже проверены на VPS.

Если все три файла совпадают с активными:
- вывод PG_CONFIG_NOOP; no restart;
- контейнер, active.json и подключённый каталог сохраняются;
- config-ci-last.json отмечает новый source_revision и старый mounted_revision.
Поэтому status может показывать разные SHA — при одинаковом содержимом это
нормально и не означает, что новый commit проигнорирован.

Если содержимое отличается:
- вызывается существующий deploy(commit, dest, report);
- штатная остановка, сохранение старого контейнера, создание нового на том же
  образе и том же PGDATA, проверка здоровья и effective paths;
- при исключении существующий deployer выполняет свой rollback;
- SIGKILL и удаление БД не добавляются.

Таким образом изменение конфигурации пока вызывает короткую остановку
PostgreSQL и замену контейнера, даже если параметр поддерживает reload.
Релизы, старые контейнеры и отчёты автоматически не удаляются.
Установка pipeline сама по себе содержимое конфигурации не меняет: первый
запуск должен завершиться NOOP.

## Ошибки и эксплуатация

    sudo /usr/local/sbin/ac-pg-config-ci status
    sudo /usr/local/sbin/ac-pgctl check
    sudo /usr/local/sbin/ac-pg-config-ci disable

disable запрещает apply, но не останавливает PostgreSQL.
prepare FULL_SHA импортирует и тестирует архив без применения.
Архив должен быть уже экспортирован в inbox доверенным CI-пользователем.

Подробности сбоя сохраняются в root-only REPORT/error.txt, когда ошибка
произошла после создания отчёта; engine также сохраняет test-settings.txt,
test-container.log и transaction.json. Не публиковать их автоматически:
диагностика может содержать параметры конфигурации.
Если pending-config-deploy.json остался, не удалять его и не повторять deploy
вслепую. Сначала проверить журнал и контейнеры; эта версия сохраняет прежний
ручной порядок восстановления после сбоя самого rollback/обрыва хоста.

Для штатного возврата настроек — git revert и новый commit в main.
Это откат конфигурации, не восстановление данных PostgreSQL.
Ручной deploy ранее неизвестного CI релиза через старый ac-pg-config может
потребовать регистрации через installer после проверки; нормальный поток
после включения CI — через config repo. При существующей квитанции drift
никогда автоматически не принимается.

Тесты пакета проверяют архивы, квитанции, drift, no-op, передачу в deployer,
отказ до deploy при ошибке preflight, сохранение прежнего статуса при ошибке,
права при umask 077 и повторную установку. Podman/visudo в unit tests
моделируются; фактический запуск на VPS проверяется первым pipeline.
