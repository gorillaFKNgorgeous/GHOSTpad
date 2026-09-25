FROM python:3.13-slim
WORKDIR /app
COPY server.py simple_server.py store.py oauth.py chat.py chat_login.py ./
RUN pip install --no-cache-dir 'openai-codex==0.156.1' \
    && useradd --system --uid 10001 ghostbridge \
    && mkdir /data \
    && chown ghostbridge /data
USER 10001
ENV BIND_HOST=0.0.0.0 PORT=8080 DATABASE_PATH=/data/ghostblender.sqlite3 CODEX_HOME=/data/codex HOME=/tmp/ghostbridge-home
EXPOSE 8080
CMD ["python", "simple_server.py"]
