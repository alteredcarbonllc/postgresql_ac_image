FROM ubuntu:24.04
    
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y \
    postgresql \
    postgresql-contrib \
    && rm -rf /var/lib/apt/lists/*

# Экспонируем стандартный порт PostgreSQL
EXPOSE 5432

# Создаем пользователя postgres
#RUN useradd -m -d /var/lib/postgresql -s /bin/bash postgres

# Подготовка каталога для данных PostgreSQL
RUN mkdir -p /var/lib/postgresql/data && \
    chown -R postgres:postgres /var/lib/postgresql


USER postgres

# Запуск PostgreSQL с указанием каталога данных
CMD ["/usr/lib/postgresql/16/bin/postgres", "-D", "/var/lib/postgresql/data"]
