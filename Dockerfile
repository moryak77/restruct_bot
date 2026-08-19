FROM python:3.11-slim

# ffmpeg нужен cogs/music.py (проигрывание музыки через yt-dlp) — без него всё остальное
# в боте работает, но плеер не запустится.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
