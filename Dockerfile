FROM python:3.12-slim

WORKDIR /srv
# The pinned dependencies go in their own layer, so editing the app doesn't reinstall them.
COPY constraints.txt .
RUN pip install --no-cache-dir -r constraints.txt
COPY . .
RUN pip install --no-cache-dir --no-deps .

ENV GAME_TASTE_DATA=/data
EXPOSE 8000

CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
