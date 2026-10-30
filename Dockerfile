FROM python:3.12-slim

WORKDIR /srv
COPY . .
RUN pip install --no-cache-dir .

ENV GAME_TASTE_DATA=/data
EXPOSE 8000

CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
