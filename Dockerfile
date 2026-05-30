FROM docker:27-cli

# tzdata: local-time scheduling; coreutils: robust date/ls; python3: web GUI + scheduler.
# docker-cli-compose: lets restore.sh run `docker compose up -d` to recreate
# containers from a saved compose file during a from-scratch restore.
RUN apk add --no-cache tzdata coreutils python3 docker-cli-compose

COPY backup.sh /usr/local/bin/backup.sh
COPY restore.sh /usr/local/bin/restore.sh
COPY app.py /usr/local/bin/app.py
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/app.py \
 && ln -s /usr/local/bin/restore.sh /usr/local/bin/restore \
 && ln -s /usr/local/bin/backup.sh /usr/local/bin/backup

EXPOSE 8088
ENTRYPOINT ["python3", "/usr/local/bin/app.py"]
